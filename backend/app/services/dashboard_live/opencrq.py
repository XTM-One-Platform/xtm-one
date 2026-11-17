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

"""OpenCRQ metrics: one query procedure of its ``dashboard`` router, and its answer.

``GET /trpc/{procedure}?input=<json>`` with the ``x-tenant-id`` header; the
procedure and its input come from the metric catalog
(``dashboard_platform.opencrq.METRICS`` / ``procedure_input``), never from the
widget. OpenCRQ scopes every query by tenant: the integration's Tenant field
when set, else the person's only tenant (several tenants and no Tenant field:
the widget asks for the Tenant field, ``tenant_required``; none: access denied).

Each metric reads its own fields of the answer (the output schemas of
``opengrc/back/src/features/app-router/dashboard/dashboard-router.ts``).
"""

from __future__ import annotations

import json
import logging

from app.services.dashboard_platform.opencrq import METRICS, procedure_input

from . import LiveError, LiveRequest, distribution_data, link, number, number_data, rows_data, series_data
from ._http import request_json

logger = logging.getLogger(__name__)


def _round(value: object) -> str:
    parsed = number(value)
    if parsed is None:
        return ""
    return f"{parsed:,.0f}" if abs(parsed) >= 100 else f"{parsed:.2f}".rstrip("0").rstrip(".")


def _list(value: object) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def normalize(metric: str, answer: object) -> dict:
    """The closed shape of *metric* from its procedure's answer."""
    if metric in ("exposure_trend_ale", "exposure_trend_lef"):
        field, name = ("ale", "Annual loss expectancy") if metric == "exposure_trend_ale" else ("lef", "Loss event frequency")
        # Levels, not counts: a merged bucket is their mean.
        return series_data([(name, [(p.get("date"), p.get(field)) for p in _list(answer)])], aggregate="mean")
    if metric == "findings_trend":
        points = _list(answer)
        return series_data(
            [
                ("New", [(p.get("date"), p.get("newCount")) for p in points]),
                ("Dismissed", [(p.get("date"), p.get("dismissedCount")) for p in points]),
            ]
        )
    if metric == "risk_by_scope":
        return distribution_data([(s.get("name"), s.get("ale")) for s in _list(answer)])
    if metric == "scenario_ale":
        rows = [
            {
                "name": s.get("name"),
                "scope": s.get("organizationName"),
                "ale": _round(_obj(s.get("latestRun")).get("aleMean")),
            }
            for s in _list(answer)
        ]
        rows.sort(key=lambda r: -(number(r["ale"].replace(",", "")) or 0.0))
        return rows_data([("name", "Scenario"), ("scope", "Scope"), ("ale", "ALE")], rows, len(rows))
    if metric == "tolerance_gaps":
        gaps = [g for g in _list(_obj(answer).get("rows")) if g.get("outside")]
        rows = [{"name": g.get("name"), "scope": g.get("organizationName"), "gap": _round(g.get("gap"))} for g in gaps]
        return rows_data([("name", "Scenario"), ("scope", "Scope"), ("gap", "Over tolerance")], rows, len(rows))
    if metric == "top_controls":
        rows = [
            {"name": c.get("name"), "ref": c.get("externalId"), "reduction": _round(c.get("totalAleReduction"))}
            for c in _list(answer)
        ]
        return rows_data([("name", "Control"), ("ref", "Reference"), ("reduction", "ALE reduction")], rows, len(rows))
    if metric == "most_active_threats":
        rows = [
            {"name": t.get("name"), "type": t.get("entityType"), "score": _round(t.get("activityScore"))}
            for t in _list(answer)
        ]
        return rows_data([("name", "Threat"), ("type", "Type"), ("score", "Activity")], rows, len(rows))
    overview = _obj(answer)
    summary = _obj(overview.get("summary"))
    counts = {
        "active_findings": "activeCount",
        "critical_findings": "criticalCount",
        "kev_findings": "kevCount",
        "impacted_assets": "impactedAssetCount",
    }
    if metric in counts:
        return number_data(summary.get(counts[metric]))
    if metric == "findings_by_severity":
        severity = _obj(summary.get("severityDistribution"))
        order = ("critical", "high", "medium", "low", "none", "unscored")
        return distribution_data([(level, severity.get(level)) for level in order if level in severity])
    if metric in ("assets_by_category", "assets_by_exposure"):
        key, label = ("byCategory", "categoryId") if metric == "assets_by_category" else ("byExposure", "exposure")
        return distribution_data([(i.get(label), i.get("assetCount")) for i in _list(overview.get(key))])
    if metric == "top_assets":
        rows = [
            {
                "name": a.get("name") or a.get("derivedName"),
                "criticality": a.get("criticality"),
                "score": _round(a.get("score")),
            }
            for a in _list(overview.get("topAssets"))
        ]
        return rows_data([("name", "Asset"), ("criticality", "Criticality"), ("score", "Score")], rows, len(rows))
    raise LiveError("unsupported")


def _unwrap(answer: object) -> object:
    """``result.data`` of a tRPC answer; an error envelope is the platform's failure."""
    if not isinstance(answer, dict):
        raise LiveError("upstream_error")
    if "error" in answer:
        code = str(_obj(_obj(answer.get("error")).get("data")).get("code") or "")
        raise LiveError("access_denied" if code in ("UNAUTHORIZED", "FORBIDDEN") else "upstream_error")
    return _obj(answer.get("result")).get("data")


async def _tenant(client) -> str:
    if client.tenant_id:
        return client.tenant_id
    answer = await request_json("GET", f"{client.base_url}/trpc/tenants.getTenants", client.headers, what="OpenCRQ")
    tenants = [t for t in _list(_unwrap(answer)) if str(t.get("id") or "").strip()]
    if len(tenants) != 1:
        logger.info("Dashboard live data: OpenCRQ integration %s has %s tenants and no Tenant field", client.integration_id, len(tenants))
        raise LiveError("tenant_required" if tenants else "access_denied")
    return str(tenants[0]["id"]).strip()[:100]


async def fetch(client, request: LiveRequest) -> dict:
    native = dict(request.native)
    metric = METRICS[native["metric"]]
    if request.range_days and metric.window:
        native["window_days"] = min(request.range_days, 366)
    payload = procedure_input(native)
    tenant = await _tenant(client)
    answer = await request_json(
        "GET",
        f"{client.base_url}/trpc/{metric.procedure}",
        {**client.headers, "x-tenant-id": tenant},
        params={"input": json.dumps(payload, separators=(",", ":"))},
        what="OpenCRQ",
    )
    return normalize(native["metric"], _unwrap(answer))


def open_url(client, native: dict) -> str | None:
    return link(client.base_url, "/")


__all__ = ["fetch", "normalize", "open_url"]
