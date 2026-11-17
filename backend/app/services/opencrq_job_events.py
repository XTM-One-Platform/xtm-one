# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""OpenCRQ events for ``integration_event`` assignments (CTEM fleets, slice S2d).

OpenCRQ has no outbound webhook or notifier: its notifications are per-user
UI rows and its ``data_changed`` channel is an internal Postgres LISTEN. What
it does expose is the run of every background job, through the tRPC procedure
``jobs.listAllRuns`` (filtered by kind, status and ``finishedFrom``, sorted by
``finishedAt``), the read the ``list_opencrq_jobs`` tool already makes. The
three CTEM events are the successful end of the jobs that produce them, so
OpenCRQ needs no change and XTM One polls (``event_poller``, every 60 s):

* ``risk_scenario_recomputed``: ``crq-simulation`` or ``crq-price-recompute``
  succeeded (a Monte Carlo run, or the daily re-pricing of every scenario);
* ``finding_score_changed``: ``finding-score-recompute`` succeeded;
* ``control_validation_ingested``: ``openaev-findings-sync`` or
  ``proven-posture-record`` succeeded (OpenAEV results ingested, proven
  posture recorded).

Each run becomes one event payload whose ``message_id`` is the run id, so the
assignment's seen set runs it once, and whose ``entity`` describes the run
(kind, status, times, totals, tenant, the OpenCRQ instance URL).
"""

from __future__ import annotations

from typing import Any

PROVIDER = "opencrq"

#: Event -> the job kinds whose successful run is that event.
EVENT_KINDS: dict[str, tuple[str, ...]] = {
    "risk_scenario_recomputed": ("crq-simulation", "crq-price-recompute"),
    "finding_score_changed": ("finding-score-recompute",),
    "control_validation_ingested": ("openaev-findings-sync", "proven-posture-record"),
}
EVENTS = frozenset(EVENT_KINDS)

#: The poll interval of these events: a recompute is minutes apart at most.
POLL_INTERVAL_SECONDS = 60

#: OpenCRQ's terminal success status (``JOB_STATUSES`` of the jobs module).
SUCCEEDED = "succeeded"


def runs_query(event_type: str, since: str, limit: int) -> dict[str, Any]:
    """The ``jobs.listAllRuns`` input: the event's kinds, succeeded, finished at or after *since*, oldest first."""
    return {
        "kinds": list(EVENT_KINDS[event_type]),
        "statuses": [SUCCEEDED],
        "finishedFrom": since,
        "sortBy": "finishedAt",
        "sortDesc": False,
        "limit": limit,
    }


def run_event(run: dict, event_type: str, *, base_url: str, tenant_id: str) -> dict[str, Any]:
    """The payload one run hands to the assignment: an id the seen set keys on and the run itself."""
    run_id = str(run.get("id") or "")
    entity = {
        "type": "opencrq_job_run",
        "id": run_id,
        "kind": run.get("kind"),
        "status": run.get("status"),
        "enqueued_at": run.get("enqueuedAt"),
        "started_at": run.get("startedAt"),
        "finished_at": run.get("finishedAt"),
        "processed_total": run.get("processedTotal"),
        "expected_total": run.get("expectedTotal"),
        "progress_message": run.get("progressMessage"),
        "triggered_by": run.get("triggeredBy"),
        "attempt": run.get("attempt"),
        "tenant_id": tenant_id,
        "instance_url": base_url,
    }
    return {
        "message_id": f"opencrq:{tenant_id}:{run_id}" if run_id else "",
        "provider": PROVIDER,
        "event": event_type,
        "entity": entity,
        "summary": f"OpenCRQ job {run.get('kind')} {run.get('status')} at {run.get('finishedAt')}",
    }


async def list_finished_runs(
    credential: str, event_type: str, since: str, *, tenant_id: str = "", limit: int
) -> tuple[list[dict], str, str, str | None]:
    """``(runs, base_url, tenant_id, error)``: the event's runs finished at or after *since*.

    *credential* is the ``base_url||token`` compound the OpenCRQ tools run on.
    With no *tenant_id*, the account's sole tenant is used; several tenants
    without a choice is an error naming them.
    """
    from app.integrations.providers.opencrq._common import _headers, _resolve_tenant, _trpc

    base_url, sep, api_token = (credential or "").partition("||")
    base_url = base_url.strip().rstrip("/")
    if not sep or not base_url or not api_token.strip():
        return [], base_url, "", "no OpenCRQ base URL and token in the credential"
    headers = _headers(api_token.strip())
    tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
    if err or not tenant:
        return [], base_url, "", err or "no OpenCRQ tenant"
    data, err = await _trpc(
        base_url,
        headers,
        "jobs.listAllRuns",
        "list job runs",
        input=runs_query(event_type, since, limit),
        tenant_id=tenant,
    )
    if err:
        return [], base_url, tenant, err
    items = (data or {}).get("items") if isinstance(data, dict) else None
    runs = [r for r in items or [] if isinstance(r, dict) and r.get("finishedAt")]
    return runs, base_url, tenant, None
