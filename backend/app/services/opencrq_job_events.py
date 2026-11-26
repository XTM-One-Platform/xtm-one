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

A batch recompute in OpenCRQ ends tens of runs within minutes, and each run
would start a whole register refresh (#1417). The poller therefore hands one
poll page to the assignment as ONE event (``coalesce_events``): the newest run
is the entity, ``covers`` lists every run of the page, and ``coalesce_key``
(tenant and event) lets the dispatcher fold a later page into a run of the
same key still waiting in the queue (``services/event_coalescing.py``). A run
that reports its progress and processed nothing (``expectedTotal`` set,
``processedTotal`` 0, a no-op recompute) is no event; a kind that never
reports progress ends at 0 whatever it did, so its runs are kept. The risk
scenario ids a job payload names travel on the entity and in ``covers``.

A ``crq-price-recompute`` run ends with a JSON summary as its progress
message (``currentStep`` on ``jobs.listAllRuns``): ``moved`` names the
scenarios it re-priced, ``movedCount`` counts them past the id cap, and
``movedAle`` (when OpenCRQ reports it) gives each one's ALE before and after.
They travel on the payload top level and on the entity as ``moved_scenario_ids``,
``moved_count`` and ``moved_ale``, merged across a coalesced page, so a trigger
filter reacts to "scenario X moved" (``FILTER_KEYS``, declared in the catalog).

A ``finding-score-recompute`` run ends the same way (OpenCRQ #232): its JSON
summary counts the findings, postures and unique findings it changed in
``changedCount``. A run whose summary says 0 changed nothing a reader can
see and is no event (#2169); the count travels as ``changed_count``, added up
across a coalesced page. A run with no summary (an older OpenCRQ) is kept.
"""

from __future__ import annotations

import json
from typing import Any

from app.services import event_coalescing

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

#: The ``trigger_config.filter`` keys of the OpenCRQ events: payload fields
#: matched by the generic payload filter (``tenant_id`` also picks the tenant read).
FILTER_KEYS: dict[str, str] = {
    "tenant_id": "The OpenCRQ tenant to read (needed when the account has several); only its runs",
    "kind": "Only runs of this job kind (or one of a list), e.g. crq-price-recompute",
    "risk_scenario_ids": "Only events whose job payload names this risk scenario id (or one of a list)",
    "moved_scenario_ids": "Only events whose re-pricing moved this risk scenario id (or one of a list)",
    "moved_count": 'How many scenarios the re-pricing moved, e.g. {"ne": 0} for a pass that moved one',
    "changed_count": "How many finding scores, postures and unique findings the recompute changed (OpenCRQ reports it)",
}


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


def risk_scenario_ids(run: dict[str, Any]) -> list[str]:
    """The risk scenario ids the run's job payload names (``riskScenarioId`` or ``riskScenarioIds``), in order."""
    payload = run.get("payload")
    if not isinstance(payload, dict):
        return []
    ids: list[str] = []
    single = payload.get("riskScenarioId")
    if isinstance(single, str) and single:
        ids.append(single)
    many = payload.get("riskScenarioIds")
    if isinstance(many, list):
        ids.extend(item for item in many if isinstance(item, str) and item and item not in ids)
    return ids


def progress_message(run: dict[str, Any]) -> str | None:
    """The run's progress message: ``currentStep`` on ``jobs.listAllRuns`` (``progressMessage`` before)."""
    for key in ("currentStep", "progressMessage"):
        value = run.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    """The JSON summary a run left as its progress message, ``{}`` when it is not one."""
    text = progress_message(run)
    if not text or not text.lstrip().startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ale(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def moved_scenarios(run: dict[str, Any]) -> tuple[list[str], int, list[dict[str, Any]]]:
    """``(ids, count, ale)``: the scenarios the run re-priced, from its summary.

    *ids* is ``moved`` (OpenCRQ caps it), *count* ``movedCount`` (the whole
    count, else the ids'), *ale* ``movedAle`` as ``{risk_scenario_id,
    ale_before, ale_after, ale_delta}`` (empty before OpenCRQ reports it).
    """
    summary = _summary(run)
    moved = summary.get("moved")
    ids: list[str] = []
    for item in moved if isinstance(moved, list) else []:
        if isinstance(item, str) and item and item not in ids:
            ids.append(item)
    count = _count(summary.get("movedCount"))
    ale: list[dict[str, Any]] = []
    moved_ale = summary.get("movedAle")
    for item in moved_ale if isinstance(moved_ale, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("riskScenarioId"), str):
            continue
        ale.append(
            {
                "risk_scenario_id": item["riskScenarioId"],
                "ale_before": _ale(item.get("aleBefore")),
                "ale_after": _ale(item.get("aleAfter")),
                "ale_delta": _ale(item.get("aleDelta")),
            }
        )
    return ids, count if count is not None else len(ids), ale


def changed_count(run: dict[str, Any]) -> int | None:
    """``changedCount`` of the run's summary: what a finding score recompute changed, ``None`` when unreported."""
    return _count(_summary(run).get("changedCount"))


def _set_changed(payload: dict[str, Any], count: int | None) -> None:
    """The changed count on the payload top level (the filter reads it) and on the entity, when reported."""
    for target in (payload, payload["entity"]):
        if count is None:
            target.pop("changed_count", None)
        else:
            target["changed_count"] = count


def _set_moved(payload: dict[str, Any], ids: list[str], count: int, ale: list[dict[str, Any]]) -> None:
    """The moved fields on the payload top level (the filter reads them) and on the entity."""
    for target in (payload, payload["entity"]):
        target["moved_scenario_ids"] = ids
        target["moved_count"] = count
        if ale:
            target["moved_ale"] = ale
        else:
            target.pop("moved_ale", None)


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
        "progress_message": progress_message(run),
        "triggered_by": run.get("triggeredBy"),
        "attempt": run.get("attempt"),
        "tenant_id": tenant_id,
        "instance_url": base_url,
    }
    scenario_ids = risk_scenario_ids(run)
    if scenario_ids:
        entity["risk_scenario_ids"] = scenario_ids
    payload: dict[str, Any] = {
        "message_id": f"opencrq:{tenant_id}:{run_id}" if run_id else "",
        "provider": PROVIDER,
        "event": event_type,
        "tenant_id": tenant_id,
        "kind": run.get("kind"),
        "risk_scenario_ids": scenario_ids,
        "entity": entity,
        "summary": f"OpenCRQ job {run.get('kind')} {run.get('status')} at {run.get('finishedAt')}",
    }
    _set_moved(payload, *moved_scenarios(run))
    _set_changed(payload, changed_count(run))
    return payload


#: Most runs one coalesced event lists in ``covers``; the count is kept whole.
MAX_COVERED_RUNS = 50

#: How the moved fields of two coalesced pages merge when one folds into the
#: other's waiting run (``event_coalescing.MERGE_KEY``, #2117).
MOVED_MERGE: dict[str, Any] = {
    "union": ["moved_scenario_ids", "risk_scenario_ids"],
    "by_key": {"moved_ale": "risk_scenario_id"},
    "count_at_least": {"moved_count": "moved_scenario_ids"},
    "sum": ["changed_count"],
}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def processed_nothing(run: dict[str, Any]) -> bool:
    """A run that reports it processed nothing: a no-op recompute, no event.

    Only a run that reports its progress counts: it declared an
    ``expectedTotal`` and ended with ``processedTotal`` 0. A job kind that
    never reports progress (``crq-price-recompute``, for one) ends every run
    with ``processedTotal`` 0 whatever it re-priced, so a 0 without an
    expected total says nothing and the run is kept.

    A run whose summary reports ``changedCount`` 0 walked its findings and
    changed none (#2169): no event either.
    """
    if changed_count(run) == 0:
        return True
    return _count(run.get("expectedTotal")) is not None and _count(run.get("processedTotal")) == 0


def coalesce_key(event_type: str, tenant_id: str) -> str:
    """The key under which the events of one tenant and one event fold into one run."""
    return f"{PROVIDER}:{tenant_id}:{event_type}"


def covered_run(run: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """One entry of a coalesced event's ``covers``; ``message_id`` is the run's own event id."""
    run_id = str(run.get("id") or "")
    return {
        "id": run_id,
        "message_id": f"opencrq:{tenant_id}:{run_id}" if run_id else "",
        "kind": run.get("kind"),
        "finished_at": run.get("finishedAt"),
        "processed_total": run.get("processedTotal"),
        "risk_scenario_ids": risk_scenario_ids(run),
        "moved_scenario_ids": moved_scenarios(run)[0],
        "changed_count": changed_count(run),
    }


def coalesce_events(
    runs: list[dict[str, Any]], event_type: str, *, base_url: str, tenant_id: str
) -> list[dict[str, Any]]:
    """One event for a poll page of *runs* (oldest first), or none.

    The runs that processed nothing are left out. The newest run left is the
    event's entity and id, so a page fetched again is skipped by the seen
    set; ``covers`` lists the runs the event stands for, ``run_count`` counts
    them all, and ``coalesce_key`` folds a later page into this one while its
    run waits in the queue.
    """
    kept = [run for run in runs if not processed_nothing(run)]
    if not kept:
        return []
    event = run_event(kept[-1], event_type, base_url=base_url, tenant_id=tenant_id)
    covers = [covered_run(run, tenant_id) for run in kept]
    event["covers"] = covers[-MAX_COVERED_RUNS:]
    event["run_count"] = len(covers)
    event["coalesce_key"] = coalesce_key(event_type, tenant_id)
    page_scenarios: list[str] = []
    for run in kept:
        page_scenarios.extend(sid for sid in risk_scenario_ids(run) if sid not in page_scenarios)
    if page_scenarios:
        event["entity"]["risk_scenario_ids"] = page_scenarios
    event["risk_scenario_ids"] = page_scenarios
    event["kind"] = sorted({str(run.get("kind")) for run in kept if run.get("kind")})
    page_moved: list[str] = []
    largest_count = 0
    page_ale: dict[str, dict[str, Any]] = {}
    for run in kept:
        ids, count, ale = moved_scenarios(run)
        page_moved.extend(sid for sid in ids if sid not in page_moved)
        largest_count = max(largest_count, count)
        # A later run's figures for a scenario replace an earlier run's.
        page_ale.update({move["risk_scenario_id"]: move for move in ale})
    # The distinct scenarios moved, never a sum over runs that re-price the same
    # ones (#2117); a run whose ids OpenCRQ capped keeps its own larger count.
    _set_moved(event, page_moved, max(len(page_moved), largest_count), list(page_ale.values()))
    reported = [count for count in (changed_count(run) for run in kept) if count is not None]
    _set_changed(event, sum(reported) if reported else None)
    event[event_coalescing.MERGE_KEY] = MOVED_MERGE
    if len(covers) > 1:
        event["summary"] = (
            f"{len(covers)} OpenCRQ {event_type} jobs succeeded, the last "
            f"{kept[-1].get('kind')} at {kept[-1].get('finishedAt')}"
        )
    return [event]


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
