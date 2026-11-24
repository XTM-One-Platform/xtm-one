"""GitHub Actions tools - workflows, runs, jobs, logs, dispatch, artifacts, secrets, variables, environments."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations import ci_logs, playwright_context
from app.integrations.file_delivery import PersistFileFn, deliver_file
from app.integrations.http import app_token_cache_key
from app.llm.base import FileRef
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize
from app.tools.base import (
    CallableToolExecutor,
    ToolDefinition,
    ToolResult,
    register_fetched_file,
    workspace_ingest_requested,
)

from . import _common
from ._common import _BASE_URL, _CONCLUSION_ICONS, _MAX_LOG_CHARS, _PROVIDER, _coerce_int, _json_headers, parse_repo

_SOURCE = "integration:github"

logger = logging.getLogger(__name__)

#: How long the answer of a job log read is kept. GitHub publishes a job's log
#: once, when the job completes, and never changes it (a re-run is a new job
#: id), so the answer of a read is reused rather than downloading tens of
#: megabytes again for the same answer (#9098).
_JOB_LOG_CACHE_TTL = 24 * 3600
#: Shape of a cached answer, part of the key: a digest of the modules that
#: build the answer, so a deploy that changes them never serves an answer cached
#: before it. A hand-bumped constant was forgotten by #12112: the retest of a
#: failures_only read kept getting the answer without the findings section for
#: the rest of the TTL (and the retest of #11270 the one without the page
#: snapshot section).
_JOB_LOG_ANSWER_VERSION = hashlib.sha256(
    b"".join(Path(module.__file__).read_bytes() for module in (ci_logs, playwright_context) if module.__file__)
    + Path(__file__).read_bytes()
).hexdigest()[:12]

#: GitHub's own cap on ``per_page`` for the workflow runs endpoints.
_MAX_RUNS_PER_PAGE = 100

#: The largest artifact zip downloaded (GitHub reports its size before the download).
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
#: Bounds of an unzipped artifact: members written, and their uncompressed total.
_MAX_ARTIFACT_MEMBERS = 2_000
_MAX_ARTIFACT_UNZIPPED_BYTES = 256 * 1024 * 1024
# An abbreviated commit SHA a dispatch input may carry (#642); 40 characters is already full.
_SHORT_SHA_RE = re.compile(r"[0-9a-fA-F]{7,39}")


def _artifact_members(data: bytes) -> tuple[list[tuple[str, bytes]], list[str], str | None]:
    """The regular files of an artifact zip as ``(path, bytes)``, the members skipped, and an error.

    Bounded before anything is read: at most :data:`_MAX_ARTIFACT_MEMBERS`
    files and :data:`_MAX_ARTIFACT_UNZIPPED_BYTES` uncompressed (the sizes
    the zip declares; ``zipfile`` refuses a member whose bytes do not match
    them). A name the workspace refuses (absolute, ``..``, a control
    character), a link and a directory are skipped, never written.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [], "Error: the artifact is not a valid zip archive."
    with archive:
        infos = archive.infolist()
        regular = [i for i in infos if not i.is_dir() and ((i.external_attr >> 16) & 0o170000) in (0, 0o100000)]
        skipped = [f"{i.filename[:120]} (not a regular file)" for i in infos if not i.is_dir() and i not in regular]
        if len(regular) > _MAX_ARTIFACT_MEMBERS:
            return [], skipped, (
                f"Error: the artifact holds {len(regular)} files, over the {_MAX_ARTIFACT_MEMBERS} unzipped at most: "
                "call again with unzip=false to write the zip itself."
            )
        total = sum(i.file_size for i in regular)
        if total > _MAX_ARTIFACT_UNZIPPED_BYTES:
            return [], skipped, (
                f"Error: the artifact unzips to {total // (1024 * 1024)} MB, over the "
                f"{_MAX_ARTIFACT_UNZIPPED_BYTES // (1024 * 1024)} MB unzipped at most: call again with unzip=false."
            )
        files: list[tuple[str, bytes]] = []
        for info in regular:
            try:
                rel = normalize(info.filename)
            except WorkspacePathError:
                skipped.append(f"{info.filename[:120]} (a name the workspace does not keep)")
                continue
            files.append((rel, archive.read(info)))
    return files, skipped, None


def _format_workflow_runs(data: dict[str, Any], *, scope: str, page: int, per_page: int) -> str:
    """One line per run (head SHA and creation time included) and the next page when more remain."""
    runs = data.get("workflow_runs") or []
    total = int(data.get("total_count") or 0)
    if not runs:
        if page > 1:
            return f"No workflow runs of {scope} on page {page} ({total} total)."
        return f"No workflow runs found for {scope}."
    first = (page - 1) * per_page + 1
    last = first + len(runs) - 1
    lines = [f"Workflow runs of {scope}: {first}-{last} of {total}, newest first:"]
    for run in runs:
        conclusion = run.get("conclusion") or run.get("status") or "?"
        icon = _CONCLUSION_ICONS.get(conclusion, "?")
        stamp = str(run.get("created_at") or "")
        created = f"{stamp[:16].replace('T', ' ')}Z" if stamp else "?"
        sha = str(run.get("head_sha") or "")[:8] or "?"
        lines.append(
            f"  {icon} #{run.get('run_number')} {run.get('name')} [{conclusion}] (run_id {run.get('id')}) "
            f"{run.get('event') or '?'} on {run.get('head_branch') or '?'} @{sha} ({created}) — {run.get('html_url')}"
        )
    if last < total:
        lines.append(f"More remain: call again with page={page + 1}.")
    return "\n".join(lines)


def _running_job_answer(job_id: int, repo: str, job: dict[str, Any]) -> str | None:
    """The answer for a job whose logs do not exist yet, or ``None`` when it completed.

    GitHub only publishes a job's log once the job completes: before that, the
    logs endpoint redirects to a blob that does not exist and the storage error
    (``BlobNotFound``) reads like a failure. It starts with ``Error:`` so the
    workspace ingest writes nothing, and it says when to come back.
    """
    status = str(job.get("status") or "")
    if not status or status == "completed":
        return None
    started = job.get("started_at")
    since = f", started {started}" if started and status == "in_progress" else ""
    return (
        f"Error: job {job_id} ({job.get('name') or 'unnamed'}) in {repo} is {status}{since}: GitHub publishes "
        "its logs once it completes. Poll get_github_ci_status (or list_github_workflow_jobs) and read the "
        "logs after it finishes."
    )


def _job_lines(jobs: list[dict[str, Any]]) -> list[str]:
    """One line per job (conclusion and job id) and, under a failed job, its failed steps."""
    lines: list[str] = []
    for j in jobs:
        conclusion = j.get("conclusion") or j.get("status", "?")
        icon = _CONCLUSION_ICONS.get(conclusion, "?")
        lines.append(f"  {icon} {j.get('name')} [{conclusion}] (job_id {j.get('id')})")
        failed_steps = [s.get("name") for s in (j.get("steps") or []) if s.get("conclusion") == "failure"]
        if failed_steps:
            lines.append(f"      failed step(s): {', '.join(s for s in failed_steps if s)}")
    return lines


async def _previous_attempt_line(jobs_of: Any, run_id: int, previous: int, repo: str) -> str:
    """Name the failed jobs of an earlier attempt while the latest one is still running.

    GitHub's ``/runs/{id}/jobs`` answers the latest attempt only: a failed
    attempt 1 vanishes the moment an automatic re-run starts. A read error or
    an attempt with no failure answers an empty string (the line is a hint,
    never a reason to fail the listing).
    """
    what = f"list jobs for run {run_id} attempt {previous} in {repo}"
    jobs, err = await jobs_of(f"attempts/{previous}/jobs", what)
    if err:
        return ""
    failed = [j for j in jobs if j.get("conclusion") in {"failure", "timed_out", "cancelled"}]
    if not failed:
        return ""
    named = ", ".join(f"{j.get('name')} [{j.get('conclusion')}] (job_id {j.get('id')})" for j in failed)
    return (
        f"\nAttempt {previous} (the latest is still running) ended with: {named}. "
        f"list_github_workflow_jobs(run_id={run_id}, attempt={previous}) lists it whole."
    )


#: The failed check runs of a run's check suite: the last source of its failed jobs when every
#: jobs endpoint answers a server error. ``checkType: ALL`` is required: without it GitHub keeps
#: the latest check run per name only and a FAILURE filter can answer none.
_FAILED_CHECK_RUNS_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on CheckSuite {
      checkRuns(first: 100, filterBy: {checkType: ALL, conclusions: [FAILURE, TIMED_OUT, CANCELLED]}) {
        nodes { databaseId name status conclusion }
      }
    }
  }
}
"""


#: The conclusions of a run that concluded without any job when its jobs list is empty: GitHub
#: never assigned it a runner (an Actions incident, 9 Nov 2026 18:09-18:28Z on two
#: organisations at once, #11490), so there is no log, no step and nothing in the code to fix.
_NO_JOBS_CONCLUSIONS = frozenset({"failure", "cancelled", "startup_failure", "timed_out"})


def no_jobs_run(run: dict[str, Any], jobs_total: int) -> bool:
    """Whether a workflow run is classified ``no_jobs``: completed, not green, and without any job."""
    return run.get("status") == "completed" and run.get("conclusion") in _NO_JOBS_CONCLUSIONS and jobs_total == 0


def _no_jobs_answer(run: dict[str, Any], run_id: int, repo: str) -> str:
    """The answer for a run classified ``no_jobs``: not a red to diagnose, and its one rerun."""
    attempt = max(1, _coerce_int(run.get("run_attempt")) or 1)
    head = (
        f"Run {run_id} in {repo} concluded {run.get('conclusion')} with no job: classified no_jobs. "
        "GitHub never started a job (no runner assigned, no log): it is not a test or workflow failure, "
        "so there is nothing to diagnose."
    )
    if attempt > 1:
        return (
            f"{head} It is attempt {attempt}: it was already re-run once, so do not re-run it again; "
            "push a new commit or wait for GitHub Actions to recover."
        )
    return (
        f"{head} Re-run it once: manage_github_workflow_run(run_id={run_id}, action='rerun') "
        "(rerun_failed is turned into a full rerun, there is no failed job to re-run)."
    )


def _is_server_error(status: int | None) -> bool:
    """True for a 5xx answer (a gateway error while GitHub swaps in a re-run's jobs, among others)."""
    return status is not None and status >= 500


def _check_run_jobs(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The failed check runs of a check suite in the jobs endpoint's shape (an Actions job id is its check-run id)."""
    suite = (data or {}).get("node") or {}
    nodes = (suite.get("checkRuns") or {}).get("nodes") or []
    return [
        {
            "id": n.get("databaseId"),
            "name": n.get("name"),
            "status": str(n.get("status") or "").lower(),
            "conclusion": str(n.get("conclusion") or "").lower() or None,
            "steps": [],
        }
        for n in nodes
        if isinstance(n, dict)
    ]


#: Annotations read for a job whose log was never published.
_MAX_ANNOTATIONS = 50


def _log_is_missing(err: str | None) -> bool:
    """True when the logs endpoint said the log does not exist (a 404 or the storage's ``BlobNotFound``).

    ``format_http_error`` writes a 404 as "resource not found while trying to ...":
    matching only "(404)" left the check-run and unpublished-log answers unreachable (#639).
    """
    if not err:
        return False
    low = err.lower()
    return "blobnotfound" in low or "(404)" in low or "resource not found" in low


def _unpublished_log_answer(job_id: int, repo: str, job: dict[str, Any], annotations: list[Any] | None) -> str:
    """The answer for a completed job whose log GitHub never published.

    A hosted runner that lost communication with the server, or a job cancelled
    before its runner started, completes without a log: the logs endpoint then
    redirects to a blob that does not exist. The cause lives in the job's steps
    and in the check-run annotations (an Actions job id is its check-run id),
    so the answer carries both instead of the storage error. ``annotations`` is
    ``None`` when they could not be read.
    """
    conclusion = job.get("conclusion") or job.get("status") or "?"
    lines = [
        f"Job {job_id} ({job.get('name') or 'unnamed'}) in {repo} completed [{conclusion}] but GitHub never "
        "published its log: no log to read. Its steps and its check-run annotations carry the cause."
    ]
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    if steps:
        lines.append("Steps:")
        for step in steps:
            state = step.get("conclusion") or step.get("status") or "?"
            lines.append(f"  {step.get('number', '?')}. {step.get('name') or 'unnamed'} [{state}]")
    lines.extend(_annotation_lines(annotations))
    return "\n".join(lines)


#: Characters of a third-party check run's output summary and text kept in the answer.
_MAX_CHECK_OUTPUT_CHARS = 4000


def _app_check_run_answer(check_id: int, repo: str, check: dict[str, Any], annotations: list[Any] | None) -> str:
    """The answer for a check run id that is not a GitHub Actions job.

    A check run of another app (CircleCI, Codecov, SonarCloud, a security
    scanner) has no Actions log: the jobs endpoint answers 404 for its id. What
    it reported lives in its own output (title, summary, text), its annotations
    and its details link, so the answer carries those instead of "Not Found".
    """
    raw_app = check.get("app")
    app: dict[str, Any] = raw_app if isinstance(raw_app, dict) else {}
    app_name = app.get("slug") or app.get("name") or "another app"
    state = check.get("conclusion") or check.get("status") or "?"
    lines = [
        f"Check run {check_id} ({check.get('name') or 'unnamed'}) in {repo} is reported by {app_name}, not by a "
        f"GitHub Actions job [{state}]: it has no Actions log. Its own output follows."
    ]
    if check.get("details_url"):
        lines.append(f"Details: {check['details_url']}")
    raw_output = check.get("output")
    output: dict[str, Any] = raw_output if isinstance(raw_output, dict) else {}
    reported = False
    for key in ("title", "summary", "text"):
        value = str(output.get(key) or "").strip()
        if not value:
            continue
        reported = True
        if len(value) > _MAX_CHECK_OUTPUT_CHARS:
            value = value[:_MAX_CHECK_OUTPUT_CHARS] + f"\n... [cut at {_MAX_CHECK_OUTPUT_CHARS:,} characters]"
        lines.append(f"{key.capitalize()}:\n{value}")
    if not reported:
        lines.append("Output: none (the app reported no title, summary or text).")
    lines.extend(_annotation_lines(annotations))
    return "\n".join(lines)


def _annotation_lines(annotations: list[Any] | None) -> list[str]:
    """The check-run annotations as answer lines; ``None`` when they could not be read."""
    lines: list[str] = []
    if annotations is None:
        lines.append("Annotations: unreadable (the check-run annotations endpoint answered an error).")
    elif not annotations:
        lines.append("Annotations: none.")
    else:
        lines.append(f"Annotations ({len(annotations)}):")
        for note in annotations[:_MAX_ANNOTATIONS]:
            if not isinstance(note, dict):
                continue
            level = note.get("annotation_level") or "?"
            title = f"{note['title']}: " if note.get("title") else ""
            path = note.get("path") or ""
            where = f" ({path}:{note.get('start_line')})" if path and path != ".github" else ""
            message = " ".join(str(note.get("message") or "").split())
            lines.append(f"  [{level}] {title}{message}{where}")
    return lines


def _job_log_cache_key(headers: dict[str, str], owner: str, name: str, job_id: int, mode: str) -> str:
    """Redis key of a job log answer: per credential digest (never the token), repository, job and read mode."""
    digest = app_token_cache_key(_PROVIDER, headers.get("Authorization", ""))[:16]
    return f"github:job_log:{_JOB_LOG_ANSWER_VERSION}:{digest}:{owner.lower()}/{name.lower()}:{job_id}:{mode}"


async def _job_log_cache_get(key: str) -> str | None:
    """The cached answer under *key*, or ``None``; fails open when Redis is unavailable."""
    try:
        from app.core.redis import redis_client

        value = await (await redis_client()).get(key)
    except Exception as exc:
        logger.debug("get_github_job_logs cache read failed: %s", format_exception_message(exc))
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) and value else None


#: Artifacts of a run read for the error-context.md of a failed Playwright test, and the largest zip read.
_MAX_CONTEXT_ARTIFACTS = 3
_MAX_CONTEXT_ARTIFACT_BYTES = 25 * 1024 * 1024
#: The largest error-context.md read from a zip.
_MAX_CONTEXT_FILE_BYTES = 512 * 1024


def _context_artifacts(artifacts: list[Any], job_name: str) -> list[dict[str, Any]]:
    """The run's Playwright output artifacts worth opening, the one whose name shares most words with the job first.

    Playwright writes ``error-context.md`` under its ``outputDir``, ``test-results``
    by default: an artifact named after it, not expired, within the size bound.
    """
    shard = re.search(r"(\d+)\s*/\s*\d+", job_name)
    job_words = set(re.findall(r"[a-z0-9]+", re.sub(r"\d+\s*/\s*\d+", " ", job_name.lower())))
    if shard:
        # Actions shows "1/6" for the strategy.job-index 0 an artifact name carries.
        job_words.add(str(int(shard.group(1)) - 1))
    usable = [
        a
        for a in artifacts
        if isinstance(a, dict)
        and "test-results" in str(a.get("name") or "").lower()
        and not a.get("expired")
        and int(a.get("size_in_bytes") or 0) <= _MAX_CONTEXT_ARTIFACT_BYTES
    ]

    def _overlap(a: dict[str, Any]) -> int:
        return len(set(re.findall(r"[a-z0-9]+", str(a.get("name") or "").lower())) & job_words)

    return sorted(usable, key=_overlap, reverse=True)[:_MAX_CONTEXT_ARTIFACTS]


def _context_lines(markdown: str) -> list[str] | None:
    """The snapshot lines of one ``error-context.md``, ``None`` when it holds no page snapshot."""
    if not playwright_context.has_page_snapshot(markdown):
        return None
    return playwright_context.snapshot_lines(markdown)


def _context_members(data: bytes, directories: list[str]) -> dict[str, str]:
    """The ``error-context.md`` of each wanted test directory found in an artifact zip, by directory."""
    found: dict[str, str] = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return found
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > _MAX_CONTEXT_FILE_BYTES:
                continue
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) < 2 or parts[-1] != "error-context.md" or parts[-2] not in directories:
                continue
            found.setdefault(parts[-2], archive.read(info).decode("utf-8", errors="replace"))
    return found


async def _playwright_contexts(
    client: Any, headers: dict[str, str], owner: str, name: str, job_id: int, log: str
) -> str:
    """The page snapshot of each failed Playwright test the log names, read from the run's artifacts.

    Empty when the log names no ``error-context.md``. When no artifact holds
    it (expired, over the bound, another name), the answer says which
    artifacts to download and the member path, never fails the log read.
    """
    named = playwright_context.error_context_paths(log)[: playwright_context.MAX_CONTEXTS]
    if not named:
        return ""
    directories = [d for _, d in named]
    repo = f"{owner}/{name}"
    found: dict[str, str] = {}
    sources: dict[str, str] = {}
    tried: list[str] = []
    job_resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{repo}/actions/jobs/{job_id}",
        _PROVIDER,
        f"get job {job_id}",
        headers=headers,
    )
    job = {} if err else job_resp.json()
    run_id = job.get("run_id") if isinstance(job, dict) else None
    if run_id:
        listed, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{repo}/actions/runs/{run_id}/artifacts",
            _PROVIDER,
            f"list artifacts of run {run_id}",
            headers=headers,
            params={"per_page": 100},
        )
        artifacts = [] if err else (listed.json() or {}).get("artifacts") or []
        for artifact in _context_artifacts(artifacts, str(job.get("name") or "")):
            tried.append(f"{artifact.get('name')} (#{artifact.get('id')})")
            raw, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{repo}/actions/artifacts/{artifact.get('id')}/zip",
                _PROVIDER,
                f"download artifact {artifact.get('id')}",
                headers=headers,
            )
            if err:
                continue
            members = await asyncio.to_thread(_context_members, raw.content, [d for d in directories if d not in found])
            found.update(members)
            for directory in members:
                sources.setdefault(directory, f"{artifact.get('name')} (#{artifact.get('id')})")
            if len(found) == len(directories):
                break
    contexts = [(d, await asyncio.to_thread(_context_lines, found[d])) for d in directories if d in found]
    out = playwright_context.format_contexts(contexts, sources) if contexts else ""
    missing = [path for path, d in named if d not in found]
    if missing:
        where = f" (artifacts opened: {', '.join(tried)})" if tried else ""
        out += (
            ("\n\n" if out else "")
            + f"error-context.md not read{where}: {', '.join(missing)}. list_github_artifacts(repo, run_id) then "
            "download_github_workflow_artifact(to_workspace_path) on the run's test-results artifact holds it."
        )
    return out


async def _job_log_cache_set(key: str, answer: str) -> None:
    """Keep *answer* under *key* for :data:`_JOB_LOG_CACHE_TTL`; a failure only costs the next read."""
    try:
        from app.core.redis import redis_client

        await (await redis_client()).set(key, answer, ex=_JOB_LOG_CACHE_TTL)
    except Exception as exc:
        logger.debug("get_github_job_logs cache write failed: %s", format_exception_message(exc))


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    """Build the GitHub Actions tools and register their callables."""

    async def list_github_workflows(repo: str, count: int = 50) -> str:
        """List the GitHub Actions workflows defined in a repository (name, state, file path)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows",
                _PROVIDER,
                f"list workflows in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            workflows = resp.json().get("workflows", [])
            return _common.format_entity_list(
                workflows,
                lambda w: f"#{w.get('id')} {w.get('name')} [{w.get('state')}] — {w.get('path')}",
                noun="workflow",
                empty_msg=f"No workflows found in {repo}.",
            )

    async def list_github_workflow_runs(
        repo: str,
        status: str = "",
        branch: str = "",
        count: int = 15,
        workflow: str = "",
        created: str = "",
        event: str = "",
        head_sha: str = "",
        page: int = 1,
    ) -> str:
        """List GitHub Actions workflow runs, newest first.

        Filter by status (queued, in_progress, completed, success, failure,
        cancelled), branch, workflow (file name or id), created (GitHub's date
        filter, e.g. ``>=2026-11-03T00:00:00Z``), event and head SHA; page
        through with ``page``. Each row carries the short head SHA and the
        creation time.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        per_page = max(1, min(count, _MAX_RUNS_PER_PAGE))
        page = max(1, page)
        params: dict[str, Any] = {"per_page": per_page, "page": page}
        filters = {"status": status, "branch": branch, "created": created, "event": event, "head_sha": head_sha}
        for key, value in filters.items():
            if value.strip():
                params[key] = value.strip()
        workflow = workflow.strip()
        path = f"workflows/{urlquote(workflow, safe='')}/runs" if workflow else "runs"
        scope = f"workflow {workflow} in {repo}" if workflow else repo
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/{path}",
                _PROVIDER,
                f"list workflow runs of {scope}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
        return _format_workflow_runs(data, scope=scope, page=page, per_page=per_page)

    async def get_github_workflow_run(repo: str, run_id: int) -> str:
        """Get details of a single workflow run: status, conclusion, trigger, timing, and URL."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}",
                _PROVIDER,
                f"get workflow run {run_id} in {repo}",
                headers=headers,
            )
            if err:
                return err
            r = resp.json()
            conclusion = r.get("conclusion") or r.get("status", "?")
            return "\n".join(
                [
                    f"{_CONCLUSION_ICONS.get(conclusion, '?')} Run #{r.get('run_number')} — {r.get('name')}",
                    f"Status: {r.get('status')} | Conclusion: {r.get('conclusion') or 'n/a'}",
                    f"Event: {r.get('event')} | Branch: {r.get('head_branch')} | Attempt: {r.get('run_attempt', 1)}",
                    f"Commit: {(r.get('head_sha') or '')[:8]} — {((r.get('head_commit') or {}).get('message') or '').splitlines()[0] if (r.get('head_commit') or {}).get('message') else ''}",
                    f"Started: {r.get('run_started_at') or r.get('created_at')} | Updated: {r.get('updated_at')}",
                    f"URL: {r.get('html_url', '')}",
                    f"Run ID: {r.get('id')} (use list_github_workflow_jobs for per-job detail, manage_github_workflow_run to re-run/cancel)",
                ]
            )

    async def list_github_workflow_jobs(repo: str, run_id: int, attempt: int = 0) -> str:
        """List the jobs of a workflow run with each job's conclusion and the step that failed (if any).

        ``attempt`` reads one attempt of the run (GitHub's
        ``/runs/{id}/attempts/{n}/jobs``); without it the latest attempt is
        read and, while a re-run attempt is still going, the failed jobs of the
        attempt before it are named so the failure stays readable.

        A server error on the latest-attempt endpoint (GitHub answers 502 for a
        while once an automatic re-run starts) is retried once, then the run's
        own attempt and the one before it are read, and last the failed check
        runs of its check suite; the answer names the source that answered.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        runs_url = f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}"
        async with egress_client(timeout=30) as client:

            async def _jobs_with_status(path: str, what: str) -> tuple[list[dict[str, Any]], str | None, int | None]:
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{runs_url}/{path}",
                    _PROVIDER,
                    what,
                    headers=headers,
                    params={"per_page": 100},
                    expected_statuses=set(),
                )
                if err:
                    return [], err, getattr(resp, "status_code", None)
                return list(resp.json().get("jobs") or []), None, None

            async def _jobs_of(path: str, what: str) -> tuple[list[dict[str, Any]], str | None]:
                jobs, err, _status = await _jobs_with_status(path, what)
                return jobs, err

            async def _after_server_error() -> tuple[list[dict[str, Any]], str, str] | None:
                """The jobs from the attempt endpoints, else the check suite's failed check runs."""
                resp, rerr = await _common.safe_request(
                    client, "get", runs_url, _PROVIDER, f"get workflow run {run_id} in {repo}", headers=headers
                )
                if rerr or resp is None:
                    return None
                run = resp.json() or {}
                current = max(1, _coerce_int(run.get("run_attempt")) or 1)
                for n in (current, current - 1):
                    if n < 1:
                        continue
                    got, aerr = await _jobs_of(
                        f"attempts/{n}/jobs", f"list jobs for run {run_id} attempt {n} in {repo}"
                    )
                    if not aerr and got:
                        which = "the run's current attempt" if n == current else "the attempt before it"
                        return got, f"run {run_id} attempt {n}", f"/attempts/{n}/jobs ({which})"
                suite = str(run.get("check_suite_node_id") or "")
                if not suite:
                    return None
                data, gerr = await _common.gh_graphql(
                    client,
                    headers,
                    _FAILED_CHECK_RUNS_QUERY,
                    {"id": suite},
                    action=f"read the failed check runs of run {run_id} in {repo}",
                )
                if gerr:
                    return None
                return (
                    _check_run_jobs(data),
                    f"run {run_id} (failed check runs of its check suite)",
                    "the check suite's failed check runs (GraphQL; no steps, the job_id is the check-run id)",
                )

            source = ""
            if attempt > 0:
                jobs, err = await _jobs_of(
                    f"attempts/{attempt}/jobs", f"list jobs for run {run_id} attempt {attempt} in {repo}"
                )
                scope = f"run {run_id} attempt {attempt}"
            else:
                what = f"list jobs for run {run_id} in {repo}"
                jobs, err, status = await _jobs_with_status("jobs", what)
                if _is_server_error(status):
                    jobs, err, status = await _jobs_with_status("jobs", what)
                scope = f"run {run_id}"
                if _is_server_error(status):
                    fallback = await _after_server_error()
                    if fallback is None:
                        return (
                            f"{err} Retried once; the attempt endpoints and the check suite did not answer "
                            "either. Call again in a minute, or with attempt=N."
                        )
                    jobs, scope, source = fallback
                    err = None
            if err:
                return err
            source_line = (
                f"Source: {source}, read because /runs/{run_id}/jobs answered a server error twice." if source else ""
            )
            if not jobs:
                if attempt <= 0 and not source:
                    resp, rerr = await _common.safe_request(
                        client, "get", runs_url, _PROVIDER, f"get workflow run {run_id} in {repo}", headers=headers
                    )
                    run = (resp.json() or {}) if not rerr and resp is not None else {}
                    if no_jobs_run(run, 0):
                        return _no_jobs_answer(run, run_id, repo)
                    current = _coerce_int(run.get("run_attempt")) or 1
                    if current > 1:
                        # A re-run attempt that has not queued its jobs yet: the empty list is
                        # attempt N's, the failure to read is attempt N-1's (#355).
                        previous = await _previous_attempt_line(_jobs_of, run_id, current - 1, repo)
                        head = f"No jobs yet for run {run_id} attempt {current} (latest) in {repo}: it has not started."
                        hint = (
                            f"\nlist_github_workflow_jobs(run_id={run_id}, attempt={current - 1}) lists the one before."
                        )
                        return head + (previous or hint)
                return "\n".join(s for s in (f"No jobs found for {scope} in {repo}.", source_line) if s)
            previous_line = ""
            latest = max(int(j.get("run_attempt") or 1) for j in jobs)
            if attempt <= 0 and latest > 1 and not source:
                scope = f"run {run_id} attempt {latest} (latest)"
                if any(j.get("status") != "completed" for j in jobs):
                    previous_line = await _previous_attempt_line(_jobs_of, run_id, latest - 1, repo)
        lines = [f"Jobs for {scope} in {repo} ({len(jobs)}):"]
        if source_line:
            lines.append(source_line)
        lines.extend(_job_lines(jobs))
        if previous_line:
            lines.append(previous_line)
        lines.append("\nUse get_github_job_logs(job_id=...) to read a failing job's logs.")
        return "\n".join(lines)

    async def get_github_job_logs(
        repo: str, job_id: int, failures_only: bool = False, tail: bool = False, patch: bool = False
    ) -> str:
        """Download a workflow job's logs: failures first, the raw tail, the diff it printed, or the whole log."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        raw_tail = tail is True or str(tail).lower() == "true"
        want_patch = patch is True or str(patch).lower() == "true"
        ingest = workspace_ingest_requested()
        # A published log never changes: a repeated read is answered from the
        # cache, not by downloading tens of megabytes again (#9098). The whole
        # log for the workspace is never cached.
        cache_key = None
        if not ingest:
            mode = "patch" if want_patch else ("tail" if raw_tail else ("failures" if failures_only else "default"))
            cache_key = _job_log_cache_key(headers, owner, name, job_id, mode)
            cached = await _job_log_cache_get(cache_key)
            if cached is not None:
                return cached
        # The logs endpoint 302-redirects to a signed plain-text URL; follow it.
        async with egress_client(timeout=60, follow_redirects=True) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/jobs/{job_id}/logs",
                _PROVIDER,
                f"get logs for job {job_id} in {repo}",
                headers=headers,
            )
            text = "" if err else (resp.text or "")
            if err or not text.strip():
                # A job still queued or running has no log yet: say so
                # instead of the storage error or "no logs".
                job_resp, job_err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/actions/jobs/{job_id}",
                    _PROVIDER,
                    f"get job {job_id} in {repo}",
                    headers=headers,
                )
                if not job_err:
                    job = job_resp.json()
                    job = job if isinstance(job, dict) else {}
                    running = _running_job_answer(job_id, repo, job)
                    if running:
                        return running
                    if job.get("status") == "completed" and (_log_is_missing(err) or not err):
                        # Completed without a published log (a lost runner):
                        # the annotations say why.
                        notes_resp, notes_err = await _common.safe_request(
                            client,
                            "get",
                            f"{_BASE_URL}/repos/{owner}/{name}/check-runs/{job_id}/annotations",
                            _PROVIDER,
                            f"get annotations of job {job_id} in {repo}",
                            headers=headers,
                            params={"per_page": _MAX_ANNOTATIONS},
                        )
                        notes = None if notes_err else notes_resp.json()
                        answer = _unpublished_log_answer(job_id, repo, job, notes if isinstance(notes, list) else None)
                        # No log to write into the workspace: an Error: prefix
                        # keeps the ingest from writing this summary as the file.
                        return f"Error: {answer}" if workspace_ingest_requested() else answer
                elif _log_is_missing(job_err):
                    # Not an Actions job: a check run of another app has no
                    # log, its own output says what it found.
                    check_resp, check_err = await _common.safe_request(
                        client,
                        "get",
                        f"{_BASE_URL}/repos/{owner}/{name}/check-runs/{job_id}",
                        _PROVIDER,
                        f"get check run {job_id} in {repo}",
                        headers=headers,
                    )
                    check = None if check_err else check_resp.json()
                    if isinstance(check, dict) and check.get("id"):
                        notes_resp, notes_err = await _common.safe_request(
                            client,
                            "get",
                            f"{_BASE_URL}/repos/{owner}/{name}/check-runs/{job_id}/annotations",
                            _PROVIDER,
                            f"get annotations of check run {job_id} in {repo}",
                            headers=headers,
                            params={"per_page": _MAX_ANNOTATIONS},
                        )
                        notes = None if notes_err else notes_resp.json()
                        answer = _app_check_run_answer(job_id, repo, check, notes if isinstance(notes, list) else None)
                        return f"Error: {answer}" if workspace_ingest_requested() else answer
            if err:
                return err
            if not text.strip():
                return f"Job {job_id} has no logs (it may not have started, or logs expired)."
            if want_patch:
                # The unified diff a formatter or generator check printed, as
                # sandbox_apply_patch takes it: no timestamp, no clipped line (#1477).
                extracted = await asyncio.to_thread(ci_logs.extract_patch, text)
                if extracted is None:
                    # Error: prefix: the ingest writes nothing to the workspace.
                    return (
                        f"Error: the log of job {job_id} in {repo} holds no unified diff "
                        "(no diff --git or ---/+++ block with a complete hunk)."
                    )
                if ingest or cache_key is None:
                    return extracted
                files = ci_logs.patch_file_count(extracted)
                body = extracted
                if len(body) > _MAX_LOG_CHARS:
                    body = body[:_MAX_LOG_CHARS] + f"\n... [cut at {_MAX_LOG_CHARS:,} of {len(extracted):,} chars]"
                answer = (
                    f"Unified diff printed by job {job_id} in {repo}: {files} file(s). to_workspace_path writes it "
                    "whole, and sandbox_apply_patch takes that file with the checkout directory.\n\n" + body
                )
                await _job_log_cache_set(cache_key, answer)
                return answer
            if ingest or cache_key is None:
                # Bound for the sandbox workspace: the whole log, uncut.
                return text
            title = f"Logs for job {job_id} in {repo}"
            if not raw_tail:
                # Default: the failure blocks first, then the log's last
                # lines (the runner's verdict). No failure cue: the tail below.
                # In a thread: a log of tens of megabytes is seconds of CPU
                # that would hold the worker's event loop (#9074).
                summarized = await asyncio.to_thread(
                    ci_logs.job_log_answer, text, title=title, failures_only=bool(failures_only)
                )
                if summarized is not None:
                    # A failed Playwright test: the page it saw, from its
                    # error-context.md, in the same answer (#11266).
                    contexts = await _playwright_contexts(client, headers, owner, name, job_id, text)
                    if contexts:
                        summarized += "\n\n" + contexts
                    await _job_log_cache_set(cache_key, summarized)
                    return summarized
            if len(text) > _MAX_LOG_CHARS:
                # Failures are usually at the end - keep the tail.
                text = (
                    f"... [truncated to last {_MAX_LOG_CHARS:,} chars of {len(text):,}; to_workspace_path gives the "
                    "whole log]\n" + text[-_MAX_LOG_CHARS:]
                )
            answer = f"Logs for job {job_id} in {repo}:\n\n{text}"
            await _job_log_cache_set(cache_key, answer)
            return answer

    def _dispatch_input_value(value: Any) -> str:
        """One workflow_dispatch input as GitHub expects it: a string, booleans lower case (#10225)."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    async def _expand_short_sha_inputs(
        client: Any, owner: str, name: str, inputs: dict[str, str]
    ) -> tuple[dict[str, str], list[str], str | None]:
        """Expand an abbreviated commit SHA given to a ref or sha input into the full 40-character SHA.

        A workflow that passes such an input to actions/checkout fetches it as a branch name, so a short
        SHA fails the fetch and reads as a red run (#642). Only inputs named like a ref or a sha whose
        value is 7 to 39 hexadecimal characters are read; one GitHub does not resolve refuses the dispatch.
        """
        expanded = dict(inputs)
        notes: list[str] = []
        for key, value in inputs.items():
            lowered = key.lower()
            if not ("ref" in lowered or "sha" in lowered) or not _SHORT_SHA_RE.fullmatch(value):
                continue
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/commits/{value}",
                _PROVIDER,
                f"resolve commit {value}",
                headers=headers,
            )
            full = ""
            if not err and resp is not None:
                try:
                    full = str((resp.json() or {}).get("sha") or "")
                except ValueError:
                    full = ""
            if len(full) != 40 or not full.startswith(value.lower()):
                return (
                    inputs,
                    notes,
                    f"Not dispatched: input '{key}' is the abbreviated SHA '{value}', which could not be resolved "
                    f"to a commit of {owner}/{name}. Pass the full 40-character SHA: a workflow checking out a "
                    "short SHA fetches it as a branch name and fails.",
                )
            expanded[key] = full
            notes.append(f"input '{key}' expanded from {value} to the full SHA {full}")
        return expanded, notes, None

    async def _inflight_runs(client: Any, owner: str, name: str, wf: str, ref: str) -> list[str] | None:
        """One line per run of the workflow on the ref not yet completed, with its job progress (#11850).

        None when the runs cannot be read: the guard never blocks a dispatch on a failed read.
        """
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{wf}/runs",
            _PROVIDER,
            f"list open runs of {wf}",
            headers=headers,
            params={"branch": ref, "per_page": 20},
        )
        if err or resp is None:
            return None
        try:
            listed = resp.json() or {}
        except ValueError:
            return None
        runs = [r for r in listed.get("workflow_runs") or [] if r.get("status") != "completed"]
        lines: list[str] = []
        for run in runs:
            run_id = run.get("id")
            progress = ""
            jobs_resp, jobs_err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}/jobs",
                _PROVIDER,
                f"list jobs of run {run_id}",
                headers=headers,
                params={"per_page": 100},
            )
            jobs: list[dict[str, Any]] | None = None
            if not jobs_err and jobs_resp is not None:
                try:
                    jobs = (jobs_resp.json() or {}).get("jobs") or []
                except ValueError:
                    jobs = None
            if jobs is not None:
                done = sum(1 for j in jobs if j.get("status") == "completed")
                passed = sum(1 for j in jobs if j.get("conclusion") in ("success", "skipped"))
                progress = f", {done}/{len(jobs)} jobs completed ({passed} passed)"
            lines.append(
                f"- run {run_id} (#{run.get('run_number')}, {run.get('event')}), {run.get('status')}, "
                f"head {(run.get('head_sha') or '')[:12]}, started {run.get('run_started_at') or run.get('created_at')}"
                f"{progress}: {run.get('html_url')}"
            )
        return lines

    async def dispatch_github_workflow(
        repo: str, workflow_id: str, ref: str, inputs: dict | None = None, cancel_in_progress: bool = False
    ) -> str:
        """Trigger a workflow_dispatch run of a workflow (by file name like 'ci.yml' or numeric ID) on a ref.

        A run of the same workflow already queued or in progress on the ref is answered instead of
        dispatching, since the workflow's concurrency group may cancel it (#11850); cancel_in_progress=True
        dispatches anyway and names the runs that may be cancelled.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if not workflow_id.strip():
            return "Error: 'workflow_id' is required (a workflow file name like 'ci.yml' or its numeric ID)."
        if not ref.strip():
            return "Error: 'ref' is required (the branch or tag to run the workflow on)."
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = {str(k): _dispatch_input_value(v) for k, v in inputs.items()}
        wf = urlquote(workflow_id.strip(), safe="")
        async with egress_client(timeout=30) as client:
            expansions: list[str] = []
            if payload.get("inputs"):
                payload["inputs"], expansions, refused = await _expand_short_sha_inputs(
                    client, owner, name, payload["inputs"]
                )
                if refused:
                    return refused
            inflight = await _inflight_runs(client, owner, name, wf, ref)
            if inflight and not cancel_in_progress:
                return (
                    f"Not dispatched: workflow '{workflow_id}' already has {len(inflight)} run(s) not completed on "
                    f"{ref} in {owner}/{name}; a new dispatch may cancel them through the workflow's concurrency "
                    "group. Follow the run below, or call again with cancel_in_progress=true to dispatch anyway.\n"
                    + "\n".join(inflight)
                )
            _resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/workflows/{wf}/dispatches",
                _PROVIDER,
                f"dispatch workflow {workflow_id} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            answer = (
                f"Dispatched workflow '{workflow_id}' on {ref} in {owner}/{name}. "
                "Use list_github_workflow_runs to track the new run."
            )
            if expansions:
                answer += "\n" + "; ".join(expansions) + "."
            if inflight:
                answer += "\nRun(s) the workflow's concurrency group may cancel:\n" + "\n".join(inflight)
            elif inflight is None:
                answer += "\nRuns in progress not checked: the run list could not be read."
            return answer

    async def manage_github_workflow_run(repo: str, run_id: int, action: str) -> str:
        """Re-run or cancel a workflow run (write).

        ``action``: ``rerun`` (re-run all jobs), ``rerun_failed`` (re-run only the
        failed jobs), or ``cancel`` (cancel an in-progress run).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        endpoint = {"rerun": "rerun", "rerun_failed": "rerun-failed-jobs", "cancel": "cancel"}.get(action)
        if not endpoint:
            return "Error: invalid action. Use rerun, rerun_failed, or cancel."
        runs_url = f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}"
        note = ""
        async with egress_client(timeout=30) as client:
            if action != "cancel":
                # A run that concluded without any job (classified no_jobs) is re-run
                # whole, and once: GitHub refuses rerun_failed on it (no failed job),
                # and a second attempt with no job is a GitHub outage a rerun loop
                # would not fix (#11490).
                run_resp, run_err = await _common.safe_request(
                    client, "get", runs_url, _PROVIDER, f"get workflow run {run_id} in {repo}", headers=headers
                )
                run = (run_resp.json() or {}) if not run_err and run_resp is not None else {}
                if run.get("status") == "completed" and run.get("conclusion") in _NO_JOBS_CONCLUSIONS:
                    jobs_resp, jobs_err = await _common.safe_request(
                        client,
                        "get",
                        f"{runs_url}/jobs",
                        _PROVIDER,
                        f"list jobs for run {run_id} in {repo}",
                        headers=headers,
                        params={"per_page": 1},
                    )
                    jobs_total = (
                        _coerce_int((jobs_resp.json() or {}).get("total_count"))
                        if not jobs_err and jobs_resp is not None
                        else None
                    )
                    if jobs_total is not None and no_jobs_run(run, jobs_total):
                        if (_coerce_int(run.get("run_attempt")) or 1) > 1:
                            return "Error: " + _no_jobs_answer(run, run_id, repo)
                        if action == "rerun_failed":
                            action, endpoint = "rerun", "rerun"
                        note = " The run concluded with no job (classified no_jobs): it was re-run whole, once."
            _resp, err = await _common.safe_request(
                client,
                "post",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}/{endpoint}",
                _PROVIDER,
                f"{action} run {run_id} in {repo}",
                headers=_json_headers(headers),
                json={},
            )
            if err:
                return err
            verb = {"rerun": "re-run", "rerun_failed": "re-run of failed jobs", "cancel": "cancellation"}[action]
            return f"Requested {verb} of workflow run {run_id} in {owner}/{name}.{note}"

    async def list_github_artifacts(repo: str, run_id: int = 0, count: int = 30) -> str:
        """List build artifacts for a repository, or for a specific run when run_id is given."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if run_id:
            url = f"{_BASE_URL}/repos/{owner}/{name}/actions/runs/{run_id}/artifacts"
        else:
            url = f"{_BASE_URL}/repos/{owner}/{name}/actions/artifacts"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"list artifacts in {repo}", headers=headers, params={"per_page": max(1, min(count, 100))}
            )
            if err:
                return err
            artifacts = resp.json().get("artifacts", [])

            def _fmt(a: dict) -> str:
                size_mb = (a.get("size_in_bytes", 0) or 0) / (1024 * 1024)
                expired = " [EXPIRED]" if a.get("expired") else ""
                return f"#{a.get('id')} {a.get('name')} ({size_mb:.2f} MB){expired} — created {(a.get('created_at') or '')[:10]}"

            listed = _common.format_entity_list(
                artifacts, _fmt, noun="artifact", empty_msg=f"No artifacts found in {repo}."
            )
            if any(not a.get("expired") for a in artifacts):
                listed += "\nTip: download_github_workflow_artifact(repo, artifact_id, to_workspace_path) unzips one into the workspace."
            return listed

    async def download_github_workflow_artifact(repo: str, artifact_id: int, unzip: bool = True) -> str | ToolResult:
        """Download a workflow run artifact: unzipped into the sandbox workspace, else the zip as a file."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        aid = _coerce_int(artifact_id)
        if not aid:
            return "Error: 'artifact_id' is required (the #id list_github_artifacts shows)."
        url = f"{_BASE_URL}/repos/{owner}/{name}/actions/artifacts/{aid}"
        # The zip is served by a redirect to a signed storage host; httpx
        # drops the Authorization header on that cross-origin hop.
        async with egress_client(timeout=180, follow_redirects=True) as client:
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"get artifact {aid} in {repo}", headers=headers
            )
            if err:
                return err
            meta = resp.json()
            meta = meta if isinstance(meta, dict) else {}
            label = str(meta.get("name") or f"artifact-{aid}")
            if meta.get("expired"):
                return f"Error: artifact {aid} ({label}) in {repo} has expired: GitHub no longer serves it."
            size = int(meta.get("size_in_bytes") or 0)
            if size > _MAX_ARTIFACT_BYTES:
                return (
                    f"Error: artifact {aid} ({label}) is {size // (1024 * 1024)} MB, over the "
                    f"{_MAX_ARTIFACT_BYTES // (1024 * 1024)} MB download limit."
                )
            raw, err = await _common.safe_request(
                client, "get", f"{url}/zip", _PROVIDER, f"download artifact {aid} in {repo}", headers=headers
            )
            if err:
                return err
            data = raw.content
        unzip_requested = unzip is True or str(unzip).lower() == "true"
        if workspace_ingest_requested() and unzip_requested:
            files, skipped, error = _artifact_members(data)
            if error:
                return error
            if not files:
                return f"Error: artifact {aid} ({label}) holds no file the workspace keeps."
            for path, content in files:
                ref = FileRef.from_bytes(
                    data=content,
                    filename=path.rsplit("/", 1)[-1],
                    content_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                )
                ref.relative_path = path
                register_fetched_file(None, ref)
            note = f"; {len(skipped)} member(s) skipped: {', '.join(skipped[:5])}" if skipped else ""
            return f"Artifact {aid} ({label}) of {repo}: {len(files)} file(s) unzipped{note}."
        return await deliver_file(
            data=data,
            filename=f"{label}.zip",
            content_type="application/zip",
            source="GitHub",
            persist_file_fn=persist_file_fn,
            links=[("Run artifacts", f"https://github.com/{owner}/{name}/actions")],
        )

    async def list_github_environments(repo: str) -> str:
        """List a repository's deployment environments and their protection rules summary."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/environments",
                _PROVIDER,
                f"list environments in {repo}",
                headers=headers,
            )
            if err:
                return err
            envs = resp.json().get("environments", [])
            return _common.format_entity_list(
                envs,
                lambda e: f"{e.get('name')}" + (f" ({len(e.get('protection_rules') or [])} protection rule(s))" if e.get("protection_rules") else ""),
                noun="environment",
                empty_msg=f"No environments found in {repo}.",
            )

    async def list_github_actions_secrets(repo: str, count: int = 100) -> str:
        """List the NAMES of a repository's Actions secrets (values are never returned by the API)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/secrets",
                _PROVIDER,
                f"list Actions secrets in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            secrets = resp.json().get("secrets", [])
            return _common.format_entity_list(
                secrets,
                lambda s: f"{s.get('name')} (updated {(s.get('updated_at') or '')[:10]})",
                noun="secret",
                empty_msg=f"No Actions secrets found in {repo}.",
            )

    async def list_github_actions_variables(repo: str, count: int = 100) -> str:
        """List a repository's Actions variables (name + value; variables are not secret)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/actions/variables",
                _PROVIDER,
                f"list Actions variables in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            variables = resp.json().get("variables", [])
            return _common.format_entity_list(
                variables,
                lambda v: f"{v.get('name')} = {v.get('value')}",
                noun="variable",
                empty_msg=f"No Actions variables found in {repo}.",
            )

    async def manage_github_actions_variable(repo: str, action: str, name: str, value: str = "") -> str:
        """Create, update, or delete a repository Actions variable (write).

        ``action``: ``create`` | ``update`` | ``delete``. Variables are NOT secret -
        do not store credentials here (use repository secrets instead).
        """
        owner, repo_name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        if not name.strip():
            return "Error: 'name' (the variable name) is required."
        base = f"{_BASE_URL}/repos/{owner}/{repo_name}/actions/variables"
        # The variable name becomes a path segment on update/delete; encode it as
        # a single segment (no slashes) so a stray '/' or reserved character can't
        # change the request path. The raw name still goes in the JSON body.
        enc_name = urlquote(name, safe="")
        async with egress_client(timeout=30) as client:
            if action == "create":
                _resp, err = await _common.safe_request(
                    client, "post", base, _PROVIDER, f"create variable {name}", headers=_json_headers(headers), json={"name": name, "value": value}
                )
                if err:
                    return err
                return f"Actions variable '{name}' created in {owner}/{repo_name}."
            if action == "update":
                _resp, err = await _common.safe_request(
                    client, "patch", f"{base}/{enc_name}", _PROVIDER, f"update variable {name}", headers=_json_headers(headers), json={"name": name, "value": value}
                )
                if err:
                    return err
                return f"Actions variable '{name}' updated in {owner}/{repo_name}."
            if action == "delete":
                _resp, err = await _common.safe_request(
                    client, "delete", f"{base}/{enc_name}", _PROVIDER, f"delete variable {name}", headers=headers
                )
                if err:
                    return err
                return f"Actions variable '{name}' deleted from {owner}/{repo_name}."
            return "Error: invalid action. Use create, update, or delete."

    executor.register("list_github_workflows", list_github_workflows)
    executor.register("list_github_workflow_runs", list_github_workflow_runs)
    executor.register("get_github_workflow_run", get_github_workflow_run)
    executor.register("list_github_workflow_jobs", list_github_workflow_jobs)
    executor.register("get_github_job_logs", get_github_job_logs)
    executor.register("dispatch_github_workflow", dispatch_github_workflow)
    executor.register("manage_github_workflow_run", manage_github_workflow_run)
    executor.register("list_github_artifacts", list_github_artifacts)
    executor.register("download_github_workflow_artifact", download_github_workflow_artifact)
    executor.register("list_github_environments", list_github_environments)
    executor.register("list_github_actions_secrets", list_github_actions_secrets)
    executor.register("list_github_actions_variables", list_github_actions_variables)
    executor.register("manage_github_actions_variable", manage_github_actions_variable)

    _repo = {"type": "string", "description": "Repository in format owner/name"}

    return [
        ToolDefinition(
            name="list_github_workflows",
            description="List the GitHub Actions workflows defined in a repository (name, active/disabled state, and .github/workflows file path).",
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "count": {"type": "integer", "description": "Max workflows (max 100)", "default": 50}},
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_workflow_runs",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "List GitHub Actions workflow runs of a repository, newest first, paged. Filter by workflow (file "
                "name such as 'e2e.yml', or id), status (success, failure, in_progress, etc.), branch, event, head "
                "SHA and creation date ('>=2026-11-03T00:00:00Z', 'A..B'). Each row: run number, workflow, "
                "conclusion, run_id, event, branch, short head SHA, creation time (UTC); the answer names the next "
                "page when more remain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "workflow": {"type": "string", "description": "Workflow file name (e.g. 'e2e.yml') or numeric id (optional)", "default": ""},
                    "status": {"type": "string", "description": "Filter: queued, in_progress, completed, success, failure, cancelled (optional)", "default": ""},
                    "branch": {"type": "string", "description": "Filter by branch name (optional)", "default": ""},
                    "created": {"type": "string", "description": "Creation date filter, GitHub syntax: '>=2026-11-03T00:00:00Z' or '2026-11-01..2026-11-03' (optional)", "default": ""},
                    "event": {"type": "string", "description": "Triggering event: push, pull_request, schedule, workflow_dispatch... (optional)", "default": ""},
                    "head_sha": {"type": "string", "description": "Only runs of this full commit SHA (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Runs per page (max 100)", "default": 15},
                    "page": {"type": "integer", "description": "Page number, from 1 (the answer names the next one)", "default": 1},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_workflow_run",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description="Get a single workflow run's status, conclusion, triggering event, commit, timing, and URL. Use the run_id from list_github_workflow_runs.",
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "run_id": {"type": "integer", "description": "Workflow run ID"}},
                "required": ["repo", "run_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_workflow_jobs",
            # State other actors move (CI, reviews, other runs): a re-read later is a new question.
            live_read=True,
            description=(
                "List the jobs of a workflow run with each job's conclusion and the names of any failed steps. Use "
                "to pinpoint which job/step failed before reading logs. Reads the latest attempt; attempt=N reads "
                "one attempt (a failed attempt 1 while an automatic re-run is going), and while the latest attempt "
                "runs the failed jobs of the one before it are named. A server error on the latest attempt falls "
                "back to the attempt endpoints, then the check suite's failed check runs (source named)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "run_id": {"type": "integer", "description": "Workflow run ID"},
                    "attempt": {
                        "type": "integer",
                        "description": "Run attempt to read (1 = the first run); default: the latest",
                    },
                },
                "required": ["repo", "run_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_job_logs",
            description=(
                "Download a workflow job's logs. Default: failures first, the failure blocks (runner errors, the test "
                "runner's FAILURES and short summary sections, linter and compiler findings) under the step that "
                "produced them, then the log's last lines; a log with no failure cue comes as its last 40,000 "
                "characters. failures_only=true drops the last lines; tail=true returns the raw last 40,000 "
                "characters whatever the log holds; patch=true only the unified diff it printed. to_workspace_path puts "
                "the whole log (or the patch) in the sandbox workspace instead. A completed job whose log was never published (a lost runner) answers its steps and "
                "check-run annotations; the id of another app's check run answers that run's output, annotations "
                "and details link. Use the job_id from list_github_workflow_jobs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "job_id": {"type": "integer", "description": "Job ID (from list_github_workflow_jobs)"},
                    "failures_only": {
                        "type": "boolean",
                        "description": "Return the failure summary alone, without the log's last lines (default false).",
                    },
                    "tail": {
                        "type": "boolean",
                        "description": "Return the raw last 40,000 characters instead of the failures (default false).",
                        "default": False,
                    },
                    "patch": {
                        "type": "boolean",
                        "description": "Only the unified diff the log printed, for sandbox_apply_patch (default false).",
                        "default": False,
                    },
                },
                "required": ["repo", "job_id"],
            },
            source=_SOURCE,
            priority=0,
            workspace_ingest=True,
        ),
        ToolDefinition(
            name="dispatch_github_workflow",
            description=(
                "Trigger a workflow_dispatch run of a workflow on a given ref. workflow_id is the workflow file name "
                "(e.g. 'ci.yml') or its numeric ID; ref is a branch or tag; inputs is an optional object of the "
                "workflow's defined inputs. The workflow must declare an 'on: workflow_dispatch' trigger."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "workflow_id": {"type": "string", "description": "Workflow file name (e.g. 'ci.yml') or numeric ID"},
                    "ref": {"type": "string", "description": "Branch or tag to run on (e.g. 'main')"},
                    "inputs": {
                        "type": "object",
                        "description": (
                            "Workflow dispatch inputs (key/value), optional. Strings, booleans and numbers are "
                            "accepted: a boolean is sent as 'true'/'false', a number as its text."
                        ),
                    },
                    "cancel_in_progress": {
                        "type": "boolean",
                        "description": "Dispatch even when a run of the workflow is in progress on the ref (default false: that run is answered)",
                        "default": False,
                    },
                },
                "required": ["repo", "workflow_id", "ref"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_github_workflow_run",
            description=(
                "Re-run or cancel a workflow run. action=rerun (all jobs), rerun_failed (only failed jobs), or cancel "
                "(stop an in-progress run). A run that concluded with no job (classified no_jobs) is re-run whole, "
                "once: a second rerun of it is refused."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "run_id": {"type": "integer", "description": "Workflow run ID"},
                    "action": {"type": "string", "enum": ["rerun", "rerun_failed", "cancel"]},
                },
                "required": ["repo", "run_id", "action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_github_artifacts",
            description="List GitHub Actions build artifacts for a repository, or for a specific workflow run when run_id is provided. Shows name, size, expiry, and creation date.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "run_id": {"type": "integer", "description": "Scope to a specific run (optional)", "default": 0},
                    "count": {"type": "integer", "description": "Max artifacts (max 100)", "default": 30},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_github_workflow_artifact",
            description=(
                "Download a GitHub Actions artifact (artifact_id from list_github_artifacts: a test report, a "
                "Playwright trace) with the integration's credential, up to 100 MB. With to_workspace_path it is "
                "unzipped into that workspace directory, its tree kept, and the files are listed; unzip=false "
                "writes the zip itself. Without it, the zip comes back as a conversation file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "artifact_id": {"type": "integer", "description": "Artifact ID (from list_github_artifacts)"},
                    "unzip": {"type": "boolean", "description": "Unzip into the workspace directory (default true)", "default": True},
                },
                "required": ["repo", "artifact_id"],
            },
            source=_SOURCE,
            priority=0,
            workspace_ingest=True,
        ),
        ToolDefinition(
            name="list_github_environments",
            description="List a repository's deployment environments and a count of their protection rules.",
            input_schema={"type": "object", "properties": {"repo": _repo}, "required": ["repo"]},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_actions_secrets",
            description="List the NAMES of a repository's Actions secrets (the API never returns secret values). Use to see which secrets are configured.",
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "count": {"type": "integer", "description": "Max secrets (max 100)", "default": 100}},
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_actions_variables",
            description="List a repository's Actions variables (name + value). Variables are non-secret configuration values used by workflows.",
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "count": {"type": "integer", "description": "Max variables (max 100)", "default": 100}},
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_actions_variable",
            description="Create, update, or delete a repository Actions variable. action=create|update|delete. Variables are NOT secret - never store credentials here.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "name": {"type": "string", "description": "Variable name"},
                    "value": {"type": "string", "description": "Variable value (create/update)", "default": ""},
                },
                "required": ["repo", "action", "name"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
