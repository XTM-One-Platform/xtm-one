"""Read-only OpenCRQ module health reads for the Platform Health module checker.

#101 S15b-2d (``dev-docs/platform-health.md`` 13.5): every count is tenant-wide
(the tenant the integration resolves), none takes or returns a connector or job
id (13.4 scope). Every procedure sent is a tRPC query; no argument selects a
write.

* ``get_opencrq_data_health``: ``riskScenarios.getRiskScenariosOverview``
  (scenarios by readiness status, the inputs missing or outdated, the ones
  never scored and the oldest last score) and ``jobs.listAllRuns`` with
  ``limit`` 1, ``total`` only (CRQ simulations enqueued in the last 30 days vs
  the 30 before, and the newest succeeded simulation with its finish time).
  The overview carries no creation time of a scenario, so growth is read on the
  CRQ simulations and says so.
* ``get_opencrq_platform_health``: ``jobs.listAllRuns`` with ``limit`` 1
  (runs moved to the dead-letter queue per day over 7 days by ``finishedAt``,
  with the daily mean; runs ``running`` for over 6 hours) and
  ``connectors.listConnectors`` (connectors by state, the failing ones, and
  the OpenCTI and OpenAEV links: configured and state). OpenCRQ exposes no
  platform error log through tRPC, so that part answers ``not exposed``.

Needs OpenCRQ with ``jobs.listAllRuns`` taking the ``enqueuedFrom`` ...
``finishedTo`` filters and ``connectors.listConnectors`` (main, Nov 2026). A
read the role cannot make answers its own line and the others still answer;
the checker reports the check that needs it as ``not_run``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import TENANT_PROP, _resolve_tenant, _trpc

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

#: The connectors whose state stands for the link to another Filigran module.
_MODULE_LINKS: Final = (("opencti", "OpenCTI"), ("openaev", "OpenAEV"))

#: A run ``running`` for longer than this is reported as stuck (13.5 ``jobs_state``).
_STUCK_AFTER: Final = timedelta(hours=6)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _count_runs(
    base_url: str,
    headers: dict[str, str],
    tenant: str,
    *,
    kinds: list[str] | None = None,
    statuses: list[str] | None = None,
    enqueued: tuple[datetime, datetime] | None = None,
    started_before: datetime | None = None,
    finished: tuple[datetime, datetime] | None = None,
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """``(total, newest run, None)`` of the runs matching, or ``(None, None, error)``.

    One ``jobs.listAllRuns`` page of one row: the count is the page's ``total``,
    the row is the newest by enqueue time.
    """
    page, err = await _trpc(
        base_url,
        headers,
        "jobs.listAllRuns",
        "count job runs",
        input={
            "kinds": kinds,
            "statuses": statuses,
            "enqueuedFrom": _iso(enqueued[0]) if enqueued else None,
            "enqueuedTo": _iso(enqueued[1]) if enqueued else None,
            "startedTo": _iso(started_before) if started_before else None,
            "finishedFrom": _iso(finished[0]) if finished else None,
            "finishedTo": _iso(finished[1]) if finished else None,
            "limit": 1,
            "offset": 0,
        },
        tenant_id=tenant,
    )
    if err:
        return None, None, err
    page = page if isinstance(page, dict) else {}
    items = [i for i in page.get("items") or [] if isinstance(i, dict)]
    total = page.get("total")
    return (total if isinstance(total, int) else len(items)), (items[0] if items else None), None


def _scenario_lines(scenarios: list[dict[str, Any]]) -> list[str]:
    if not scenarios:
        return ["Risk scenarios: none."]
    statuses = Counter(str((s.get("readiness") or {}).get("status") or "unknown") for s in scenarios)
    missing: Counter[str] = Counter()
    outdated: Counter[str] = Counter()
    for s in scenarios:
        readiness = s.get("readiness") or {}
        missing.update(str(k) for k in readiness.get("missing") or [])
        outdated.update(str(k) for k in readiness.get("outdated") or [])
    scored = [str((s.get("latestRun") or {}).get("createdAt") or "") for s in scenarios]
    never = sum(1 for at in scored if not at)
    dated = sorted(at for at in scored if at)
    by_status = ", ".join(f"{k} {v}" for k, v in sorted(statuses.items()))
    lines = [f"Risk scenarios: total {len(scenarios)} ({by_status})"]
    lines.append(
        "Inputs missing: "
        + (", ".join(f"{k} {v}" for k, v in sorted(missing.items())) or "none")
        + "; inputs outdated since the last score: "
        + (", ".join(f"{k} {v}" for k, v in sorted(outdated.items())) or "none")
    )
    lines.append(
        f"Never scored: {never}" + (f"; oldest last score {dated[0][:16]}, newest {dated[-1][:16]}" if dated else "")
    )
    return lines


def _connector_lines(connectors: list[dict[str, Any]]) -> list[str]:
    states = Counter(str(c.get("state") or "unknown") for c in connectors)
    lines = ["Connectors: " + (", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "none")]
    failing = sorted(str(c.get("source") or "?") for c in connectors if c.get("state") == "failing")
    lines.append("Connectors failing: " + (", ".join(failing) or "none"))
    by_source = {str(c.get("source")): c for c in connectors}
    for source, label in _MODULE_LINKS:
        card = by_source.get(source)
        if card is None:
            lines.append(f"{label} link: not offered by this OpenCRQ")
            continue
        configured = bool(card.get("platformConfigured")) and bool(card.get("tenantConfigured"))
        finished = (card.get("lastRun") or {}).get("finishedAt")
        lines.append(
            f"{label} link: configured {str(configured).lower()}, state {card.get('state') or 'unknown'}"
            + (f", last run finished {str(finished)[:16]}" if finished else "")
        )
    return lines


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def get_opencrq_data_health(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return f"OpenCRQ data health: {err}"
        tenant = tenant or ""
        now = datetime.now(UTC)
        lines = ["OpenCRQ data health:"]
        scenarios, err = await _trpc(
            base_url,
            headers,
            "riskScenarios.getRiskScenariosOverview",
            "read scenario readiness",
            input={},
            tenant_id=tenant,
        )
        if err:
            lines.append(f"Risk scenarios: {err}")
        else:
            lines += _scenario_lines([s for s in scenarios or [] if isinstance(s, dict)])
        kinds = ["crq-simulation"]
        recent, _, err = await _count_runs(
            base_url, headers, tenant, kinds=kinds, enqueued=(now - timedelta(days=30), now)
        )
        before, _, err_before = await _count_runs(
            base_url, headers, tenant, kinds=kinds, enqueued=(now - timedelta(days=60), now - timedelta(days=30))
        )
        if err or err_before:
            lines.append(f"CRQ simulations per 30 days: {err or err_before}")
        else:
            lines.append(f"CRQ simulations enqueued: last 30 days {recent}, the 30 days before {before}")
        _, newest, err = await _count_runs(base_url, headers, tenant, kinds=kinds, statuses=["succeeded"])
        if err:
            lines.append(f"Last successful CRQ simulation: {err}")
        elif newest is None:
            lines.append("Last successful CRQ simulation: none")
        else:
            lines.append(f"Last successful CRQ simulation: finished {str(newest.get('finishedAt') or 'unknown')[:16]}")
        lines.append("Scenario creation per 30 days: not exposed by the OpenCRQ overview (growth read on simulations).")
        return "\n".join(lines)

    async def get_opencrq_platform_health(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return f"OpenCRQ platform health: {err}"
        tenant = tenant or ""
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        lines = ["OpenCRQ platform health:"]
        per_day: list[int] = []
        failure = ""
        for back in range(6, -1, -1):
            start = day_start - timedelta(days=back)
            total, _, err = await _count_runs(
                base_url, headers, tenant, statuses=["dlq"], finished=(start, min(start + timedelta(days=1), now))
            )
            if err:
                failure = err
                break
            per_day.append(total or 0)
        if failure:
            lines.append(f"Runs failed (dead-letter queue): {failure}")
        else:
            mean = sum(per_day) / len(per_day)
            lines.append(
                "Runs failed (dead-letter queue) per day, oldest first: "
                + ", ".join(str(n) for n in per_day)
                + f"; 7-day daily mean {mean:.1f}; today {per_day[-1]}"
            )
        stuck, _, err = await _count_runs(
            base_url, headers, tenant, statuses=["running"], started_before=now - _STUCK_AFTER
        )
        lines.append(f"Runs running for over 6 h: {err or stuck}")
        connectors, err = await _trpc(
            base_url, headers, "connectors.listConnectors", "read connector states", tenant_id=tenant
        )
        if err:
            lines.append(f"Connectors: {err}")
        else:
            lines += _connector_lines([c for c in connectors or [] if isinstance(c, dict)])
        lines.append("Platform error log: not exposed by the OpenCRQ API.")
        return "\n".join(lines)

    executor.register("get_opencrq_data_health", get_opencrq_data_health)
    executor.register("get_opencrq_platform_health", get_opencrq_platform_health)

    return [
        ToolDefinition(
            name="get_opencrq_data_health",
            description="Read-only: OpenCRQ scenario readiness, scenarios never scored, CRQ simulations per 30 days, last success.",
            input_schema={"type": "object", "properties": {**TENANT_PROP}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_platform_health",
            description="Read-only: OpenCRQ failed jobs per day over 7 days, jobs stuck, connector states, OpenCTI/OpenAEV links.",
            input_schema={"type": "object", "properties": {**TENANT_PROP}, "required": []},
            source=_SOURCE,
        ),
    ]
