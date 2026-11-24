"""OpenCRQ background job tools.

* ``list_opencrq_jobs``  - background runs (CRQ simulations, syncs, recomputes).
* ``get_opencrq_job``    - one job's status and times, with its run detail (kind, finish
  time, progress, current step, the risk scenario ids its payload names) when
  the caller may read ``jobs.getRun``.
* ``manage_opencrq_job`` - cancel a job or retry one from the DLQ.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.opencrq_job_events import risk_scenario_ids
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    JOB_STATUSES,
    TENANT_PROP,
    _clean_ids,
    _one_line,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_JOB_ACTIONS = ["cancel", "retry"]


def _run_scenario_ids(run: dict[str, Any]) -> list[str]:
    """The risk scenario ids a run's job payload names; ``jobs.getRun`` serves the payload as JSON text."""
    payload: Any = run.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            # A payload over OpenCRQ's cap is cut (it ends with an ellipsis): no ids to read.
            return []
    return risk_scenario_ids({"payload": payload})


def _fmt_job(j: dict[str, Any], run: dict[str, Any] | None = None) -> str:
    """The job's status and times; *run* (``jobs.getRun``) adds kind, finish, progress and scenarios."""
    lines = [f"[Job] id: {j.get('id', 'N/A')}"]
    if run and run.get("kind"):
        lines.append(f"  Kind: {_one_line(run['kind'], 60)}")
    lines.append(f"  Status: {j.get('status', 'unknown')}")
    rows = [("enqueuedAt", "Enqueued"), ("startedAt", "Started")]
    source = {**j, **(run or {})}
    if run:
        rows.append(("finishedAt", "Finished"))
    for key, field in rows:
        if source.get(key) is not None:
            lines.append(f"  {field}: {source[key]}")
    if not run:
        return "\n".join(lines)
    # ``is not None``: an expectedTotal of 0 is a progress to show.
    if run.get("expectedTotal") is not None:
        lines.append(f"  Progress: {run.get('processedTotal', 0)}/{run['expectedTotal']}")
    elif run.get("processedTotal") is not None:
        lines.append(f"  Processed: {run['processedTotal']}")
    if run.get("attempt") is not None and run.get("maxAttempts") is not None:
        lines.append(f"  Attempt: {run['attempt']}/{run['maxAttempts']}")
    if run.get("currentStep"):
        lines.append(f"  Current step: {_one_line(run['currentStep'], 300)}")
    if run.get("lastError"):
        lines.append(f"  Last error: {_one_line(run['lastError'], 300)}")
    scenario_ids = _run_scenario_ids(run)
    if scenario_ids:
        lines.append(f"  Risk scenario ids: {', '.join(scenario_ids)}")
    return "\n".join(lines)


def _fmt_job_row(j: dict) -> str:
    # ``is not None``: an expectedTotal of 0 is a progress to show.
    progress = (
        f", progress {j.get('processedTotal', 0)}/{j['expectedTotal']}"
        if j.get("expectedTotal") is not None
        else ""
    )
    return (
        f"- {_one_line(j.get('kind'), 60, 'unknown')}: {_one_line(j.get('status'), 20, 'unknown')} "
        f"(enqueued {_one_line(j.get('enqueuedAt'), 40, 'unknown')}{progress}, id: {j.get('id')})"
        + (f" - {_one_line(j['lastError'], 120)}" if j.get("lastError") else "")
    )


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_jobs(
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        tenant_id: str = "",
    ) -> str:
        kinds = _clean_ids(kinds)
        statuses = [s.lower() for s in _clean_ids(statuses)]
        bad = [s for s in statuses if s not in JOB_STATUSES]
        if bad:
            return "statuses must be among: " + ", ".join(JOB_STATUSES) + "."
        if not 1 <= limit <= 200:
            return "limit must be between 1 and 200."
        if offset < 0:
            return "offset must be >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        page, err = await _trpc(
            base_url, headers, "jobs.listAllRuns", "list jobs",
            input={
                "kinds": kinds or None,
                "statuses": statuses or None,
                "limit": limit,
                "offset": offset,
            },
            tenant_id=tenant,
        )
        if err:
            return err
        jobs = (page or {}).get("items") or []
        total = (page or {}).get("total", len(jobs))
        if not jobs:
            return "No background jobs matched."
        return _quarantined(
            f"Background jobs ({total}, showing {len(jobs)} from {offset}):",
            "\n".join(_fmt_job_row(j) for j in jobs),
            "background jobs (last errors of connector and sync runs)",
        )

    async def get_opencrq_job(job_id: str = "", tenant_id: str = "") -> str:
        if not job_id:
            return "job_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        job, err = await _trpc(
            base_url, headers, "jobs.getJob", "get job",
            input={"id": job_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not job:
            return f"Job {job_id} not found."
        # The run detail needs MANAGE_SECURITY or platform access, like list_opencrq_jobs:
        # without it the status and times above are still the answer.
        run, run_err = await _trpc(
            base_url,
            headers,
            "jobs.getRun",
            "get job run",
            input={"id": job_id},
            tenant_id=tenant or "",
        )
        if run_err or not isinstance(run, dict):
            return _fmt_job(job)
        return _quarantined(
            f"Job {job_id}:",
            _fmt_job(job, run),
            "background job (current step and last error of a connector or sync run)",
        )

    async def manage_opencrq_job(
        action: str = "", job_id: str = "", tenant_id: str = ""
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _JOB_ACTIONS:
            return "action must be cancel or retry."
        if not job_id:
            return "job_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        procedure = (
            "jobs.cancelJob" if action == "cancel" else "jobs.retryFromDlq"
        )
        _, err = await _trpc(
            base_url, headers, procedure, f"{action} job",
            input={"id": job_id}, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return (
            f"Requested cancellation of job {job_id}."
            if action == "cancel"
            else f"Retrying job {job_id} from the dead-letter queue."
        )

    executor.register("list_opencrq_jobs", list_opencrq_jobs)
    executor.register("get_opencrq_job", get_opencrq_job)
    executor.register("manage_opencrq_job", manage_opencrq_job)

    return [
        ToolDefinition(
            name="list_opencrq_jobs",
            description=(
                "List the background runs of an OpenCRQ tenant (CRQ "
                "simulations, connector and CVE syncs, recomputes), newest "
                "first, optionally filtered by kinds (e.g. crq-simulation, "
                "cve-epss-sync) and statuses. Needs the MANAGE_SECURITY "
                "capability or platform access."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Job kinds to keep (crq-simulation, "
                            "cve-epss-sync, cve-cvss-sync, cve-kev-sync, "
                            "drata-sync...)."
                        ),
                    },
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string", "enum": JOB_STATUSES},
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 20,
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_job",
            description=(
                "Get an OpenCRQ background job's status and times (use after "
                "run_opencrq_crq_simulation to see whether it is queued, "
                "running or finished); with MANAGE_SECURITY or platform "
                "access also its kind, finish time, progress, current step, "
                "last error and the risk scenario ids it covers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job UUID."},
                    **TENANT_PROP,
                },
                "required": ["job_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_job",
            description=(
                "Cancel a running OpenCRQ background job or retry a failed "
                "one from the dead-letter queue. Confirm with the user "
                "before applying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _JOB_ACTIONS,
                    },
                    "job_id": {"type": "string", "description": "Job UUID."},
                    **TENANT_PROP,
                },
                "required": ["action", "job_id"],
            },
            source=_SOURCE,
        ),
    ]
