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

"""OpenCRQ figures in an XTM One dashboard.

OpenCRQ has no custom dashboards: its dashboards are pages of its web app, and
the only stored choice is which one opens first. What it does expose is a set
of read-only aggregate queries on its ``dashboard`` tRPC router
(``opengrc/back/src/features/app-router/dashboard/dashboard-router.ts``). A
platform widget for OpenCRQ names one **metric** of the catalog below - never a
procedure: each metric is bound to one query procedure, the input it sends and
the shape it draws, so a widget can neither call the router's mutation
(``setTenantHomeDashboard``) nor send an input the procedure does not declare.

The configuration is ``{metric, organization_id?, window_days?}``: the scope (an
OpenCRQ organization) and, for the metrics over time, how many days back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import NativeCheck, NativeError


@dataclass(frozen=True, slots=True)
class Metric:
    label: str
    #: The tRPC query procedure (``dashboard.<name>``).
    procedure: str
    shape: str
    #: Whether the procedure takes ``windowDays`` (and whether it requires it).
    window: bool = False
    window_required: bool = False


#: The catalog. Adding a metric means adding its normalizer in the live data
#: layer too (``dashboard_live.opencrq``), which reads the answer's fields.
METRICS: dict[str, Metric] = {
    "exposure_trend_ale": Metric("Annual loss expectancy over time", "dashboard.getExposureTrend", "series", window=True),
    "exposure_trend_lef": Metric("Loss event frequency over time", "dashboard.getExposureTrend", "series", window=True),
    "scenario_ale": Metric("Risk scenarios by annual loss expectancy", "dashboard.getScenarioSummaries", "rows", window=True),
    "risk_by_scope": Metric(
        "Annual loss expectancy by scope", "dashboard.getRiskScopeFigures", "distribution", window=True, window_required=True
    ),
    "tolerance_gaps": Metric("Scenarios outside risk tolerance", "dashboard.getToleranceGaps", "rows"),
    "top_controls": Metric("Controls that reduce risk the most", "dashboard.getTopControls", "rows"),
    "most_active_threats": Metric("Most active threats", "dashboard.getMostActiveThreats", "rows"),
    "active_findings": Metric("Active findings", "dashboard.getFindingsOverview", "number"),
    "critical_findings": Metric("Critical findings", "dashboard.getFindingsOverview", "number"),
    "kev_findings": Metric("Known exploited vulnerabilities", "dashboard.getFindingsOverview", "number"),
    "impacted_assets": Metric("Impacted assets", "dashboard.getFindingsOverview", "number"),
    "findings_by_severity": Metric("Findings by severity", "dashboard.getFindingsOverview", "distribution"),
    "findings_trend": Metric("New and dismissed findings over time", "dashboard.getFindingsTrend", "series"),
    "assets_by_category": Metric("Assets by category", "dashboard.getAssetsOverview", "distribution"),
    "assets_by_exposure": Metric("Assets by exposure", "dashboard.getAssetsOverview", "distribution"),
    "top_assets": Metric("Most exposed assets", "dashboard.getAssetsOverview", "rows"),
}

DEFAULT_WINDOW_DAYS = 90


def procedure_input(native: dict) -> dict:
    """The input sent to the metric's procedure, built from the stored configuration only."""
    metric = METRICS[native["metric"]]
    payload: dict = {}
    if native.get("organization_id") and metric.procedure != "dashboard.getRiskScopeFigures":
        payload["organizationId"] = native["organization_id"]
    if metric.window:
        payload["windowDays"] = native.get("window_days", DEFAULT_WINDOW_DAYS)
    return payload


def check_native(native: object) -> NativeCheck:
    """Validate an OpenCRQ metric configuration."""
    if not isinstance(native, dict):
        raise NativeError("An OpenCRQ widget must be an object with a 'metric'.")
    unknown = set(native) - {"metric", "organization_id", "window_days"}
    if unknown:
        raise NativeError(f"The OpenCRQ widget has unknown keys: {', '.join(sorted(unknown))}.")
    name = native.get("metric")
    if name not in METRICS:
        raise NativeError(f"OpenCRQ metric must be one of: {', '.join(METRICS)}.")
    metric = METRICS[name]
    out: dict = {"metric": name}
    if native.get("organization_id") is not None:
        try:
            out["organization_id"] = str(uuid.UUID(str(native["organization_id"])))
        except ValueError:
            raise NativeError("organization_id must be the id of an OpenCRQ organization (a UUID).") from None
    if native.get("window_days") is not None:
        days = native["window_days"]
        if not metric.window:
            raise NativeError(f"The {name} metric does not take window_days.")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            raise NativeError("window_days must be a whole number of days between 1 and 366.")
        out["window_days"] = days
    elif metric.window_required:
        out["window_days"] = DEFAULT_WINDOW_DAYS
    return NativeCheck(native=out, shape=metric.shape, native_type=name, title=metric.label)  # type: ignore[arg-type]


def describe(widget_type: str | None = None) -> dict:
    """OpenCRQ's metrics, or one of them with an example configuration."""
    if widget_type is None:
        return {"types": [{"type": name, "label": m.label, "draws": m.shape, "windowDays": m.window} for name, m in METRICS.items()]}
    if widget_type not in METRICS:
        raise NativeError(f"OpenCRQ metric must be one of: {', '.join(METRICS)}.")
    metric = METRICS[widget_type]
    example: dict = {"metric": widget_type}
    if metric.window:
        example["window_days"] = DEFAULT_WINDOW_DAYS
    return {
        "type": widget_type,
        "label": metric.label,
        "draws": metric.shape,
        "example": example,
        "rules": [
            "organization_id (an OpenCRQ organization's id) narrows it to one scope; without it, every scope.",
            "window_days (1-366) is how many days back a metric over time reads." if metric.window else "It takes no window_days.",
        ],
    }


def form() -> dict:
    """The editor of an OpenCRQ widget (:mod:`.forms`): one metric of the catalog, and its window when it takes one.

    A new metric keeps the organization; its window only when it takes one.
    """
    from .forms import attr, field, not_, truthy

    return {
        "version": 1,
        "typePath": "metric",
        "types": [
            {"value": name, "label": metric.label, "attrs": {"window": metric.window}, "new": {"metric": name}} for name, metric in METRICS.items()
        ],
        "default": {"type": next(iter(METRICS))},
        "fields": [
            field(
                "row",
                fields=[
                    field(
                        "select",
                        id="type",
                        label="Metric",
                        path="metric",
                        options=[{"value": name, "label": metric.label} for name, metric in METRICS.items()],
                        onChange=[{"if": not_(truthy(attr("window"))), "then": [{"remove": "window_days"}]}],
                    ),
                    field("number", label="Days", path="window_days", default=DEFAULT_WINDOW_DAYS, min=1, max=366, empty="min", when=truthy(attr("window"))),
                ],
            )
        ],
    }


__all__ = ["DEFAULT_WINDOW_DAYS", "METRICS", "Metric", "check_native", "form", "procedure_input"]
