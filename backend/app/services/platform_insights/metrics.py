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

"""``query_platform_metrics``: the backend's own ``xtmone_*`` Prometheus series, read by an agent.

The series ``/metrics`` exports (``core/metrics.py`` collectors: the sandbox
create histogram, the queue gauges...)
are scraped inside the cluster only, so no agent could verify them. This reader
serves them from the in-process ``prometheus_client`` registry as tidy rows:

- a counter: one row per label set with its current value;
- a gauge: one row per label set with its value;
- a histogram: one row per label set with ``count``, ``sum``, ``mean`` and the
  ``p50`` / ``p95`` estimates interpolated from its buckets the way
  ``histogram_quantile`` does.

Only the ``xtmone_`` families are served (no arbitrary PromQL, no process or
Python runtime series); an unknown or other series is refused by name. The view
is PROCESS-LOCAL: the values of the worker that answers the call, since that
process started (``core/metrics.py``: one pod runs several workers and the
deployment several pods). Every result says so, with the host and build that
answered. A caller outside the administrator scope reads no series.
"""

from __future__ import annotations

import math
import os
import socket
from collections.abc import Iterable
from typing import Any

from prometheus_client import REGISTRY
from prometheus_client.registry import CollectorRegistry

from .scope import InsightRefusedError, InsightScope

#: The families an agent may read: the platform's own, never the runtime's.
SERIES_PREFIX = "xtmone_"
#: The quantiles a histogram row estimates from its buckets.
QUANTILES = (("p50", 0.5), ("p95", 0.95))
#: Rows served at most per call (a label set per row).
MAX_ROWS = 500

_SERVED_TYPES = ("counter", "gauge", "histogram")


def _families(registry: CollectorRegistry) -> list[Any]:
    """The ``xtmone_`` metric families of *registry*, sorted by name."""
    found = [family for family in registry.collect() if family.name.startswith(SERIES_PREFIX)]
    return sorted(found, key=lambda family: family.name)


def _exported_name(family: Any) -> str:
    # A counter family is named without its ``_total`` suffix; the exposition
    # (and every query the fleet writes) names it with it.
    return f"{family.name}_total" if family.type == "counter" else family.name


def _find(registry: CollectorRegistry, series: str) -> Any:
    name = series.strip()
    if not name.startswith(SERIES_PREFIX):
        raise InsightRefusedError(
            f"Error: '{name}' is not served: only the platform's own {SERIES_PREFIX}* series can be read "
            "(no runtime, process or arbitrary PromQL series). Call without 'series' to list them."
        )
    families = _families(registry)
    for family in families:
        if name in (family.name, _exported_name(family)):
            return family
    known = ", ".join(_exported_name(family) for family in families) or "none"
    raise InsightRefusedError(f"Error: unknown series '{name}'. Known {SERIES_PREFIX}* series: {known}.")


def _matches(labels: dict[str, str], wanted: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in wanted.items())


def _finite(value: float) -> float | None:
    return None if math.isnan(value) or math.isinf(value) else value


def estimate_quantile(q: float, buckets: Iterable[tuple[float, float]]) -> float | None:
    """The *q* quantile of cumulative *buckets* ``(upper_bound, count)``, as ``histogram_quantile``.

    Linear interpolation inside the bucket the rank falls in (the lower bound
    of the first bucket is 0 when its upper bound is positive); a rank in the
    ``+Inf`` bucket answers the highest finite bound. ``None`` without
    observations.
    """
    ordered = sorted(buckets)
    if not ordered or ordered[-1][1] <= 0:
        return None
    total = ordered[-1][1]
    rank = q * total
    previous_bound, previous_count = 0.0, 0.0
    for bound, count in ordered:
        if count >= rank:
            if math.isinf(bound):
                return previous_bound
            if count == previous_count:
                return bound
            lower = previous_bound if previous_bound or bound > 0 else bound
            return lower + (bound - lower) * (rank - previous_count) / (count - previous_count)
        previous_bound, previous_count = bound, count
    return previous_bound


def _histogram_rows(family: Any, wanted: dict[str, str]) -> list[dict[str, Any]]:
    groups: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for sample in family.samples:
        labels = {key: value for key, value in sample.labels.items() if key != "le"}
        if not _matches(labels, wanted):
            continue
        key = tuple(sorted(labels.items()))
        group = groups.setdefault(key, {"labels": labels, "buckets": [], "count": 0.0, "sum": 0.0})
        if sample.name.endswith("_bucket"):
            group["buckets"].append((float(sample.labels["le"]), float(sample.value)))
        elif sample.name.endswith("_count"):
            group["count"] = float(sample.value)
        elif sample.name.endswith("_sum"):
            group["sum"] = float(sample.value)
    rows = []
    for group in groups.values():
        count = group["count"]
        row: dict[str, Any] = {
            "series": family.name,
            "labels": group["labels"],
            "count": int(count),
            "sum": round(group["sum"], 6),
            "mean": round(group["sum"] / count, 6) if count else None,
        }
        for label, q in QUANTILES:
            estimate = estimate_quantile(q, group["buckets"])
            row[label] = round(estimate, 6) if estimate is not None else None
        rows.append(row)
    return rows


def _value_rows(family: Any, wanted: dict[str, str]) -> list[dict[str, Any]]:
    exported = _exported_name(family)
    rows = []
    for sample in family.samples:
        if sample.name != exported or not _matches(sample.labels, wanted):
            continue
        rows.append({"series": exported, "labels": dict(sample.labels), "value": _finite(float(sample.value))})
    return rows


def _view() -> dict[str, Any]:
    from app.services.rollout_counters import build_version

    return {
        "kind": "process_local",
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "version": build_version(),
    }


def _caveats(view: dict[str, Any]) -> list[str]:
    return [
        (
            f"Process-local view: the values of the one backend worker that answered (host {view['host']}, "
            f"pid {view['pid']}, build {view['version']}) since it started, not the cluster-wide sum. Other "
            "workers and pods hold their own counts; a restart starts them again from zero. Say so."
        ),
        (
            "Gauges /metrics computes at scrape time (queue depth, quotas, relays, sandbox workspaces) hold the "
            "value of this worker's last scrape, or nothing when it never served one."
        ),
        "Histogram p50 / p95 are bucket estimates (linear inside a bucket, as histogram_quantile), not exact.",
    ]


def _labels_arg(value: Any) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise InsightRefusedError("Error: 'labels' is an object of exact label values, e.g. {\"version\": \"abc\"}.")
    return {str(key): str(item) for key, item in value.items()}


def read_metrics(
    series: str | None, labels: dict[str, str] | None = None, *, registry: CollectorRegistry | None = None
) -> dict[str, Any]:
    """Rows of *series* (or the list of served series when empty) from *registry* (default the process's)."""
    registry = registry or REGISTRY
    view = _view()
    if not series:
        listed = [
            {
                "series": _exported_name(family),
                "type": family.type,
                "documentation": family.documentation,
                "label_sets": len({tuple(sorted(s.labels.items())) for s in family.samples if "le" not in s.labels}),
            }
            for family in _families(registry)
            if family.type in _SERVED_TYPES
        ]
        return {"view": view, "series": listed, "caveats": _caveats(view)}
    family = _find(registry, series)
    if family.type not in _SERVED_TYPES:
        raise InsightRefusedError(f"Error: '{series}' is a {family.type}; only counters, gauges and histograms are served.")
    wanted = labels or {}
    rows = _histogram_rows(family, wanted) if family.type == "histogram" else _value_rows(family, wanted)
    rows.sort(key=lambda row: sorted(row["labels"].items()))
    truncated = len(rows) > MAX_ROWS
    return {
        "view": view,
        "series": _exported_name(family),
        "type": family.type,
        "documentation": family.documentation,
        "rows": rows[:MAX_ROWS],
        "truncated": truncated,
        "caveats": _caveats(view),
    }


async def query_platform_metrics(db: Any, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    """The tool's read: administrators only; anyone else gets their scope and no series."""
    del db  # The registry is in memory: the session the suite opens is not needed.
    if not scope.platform:
        return {
            **scope.header(),
            "rows": [],
            "caveats": ["The platform's Prometheus series are served to an administrator's agent only."],
        }
    series = str(args.get("series") or "").strip()
    return {**scope.header(), **read_metrics(series, _labels_arg(args.get("labels")))}
