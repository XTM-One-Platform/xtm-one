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

"""STIX temporal and date-diff analytics tool for FIMI analysis.

Computes timestamp diffs, detects timeline drift, and produces windowed
trend summaries across STIX objects.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

from dateutil import parser as dtparser

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import normalize_tool_error

_FIMI_SOURCE = "builtin:cti"

_TIMESTAMP_FIELDS = ("created", "modified", "first_seen", "last_seen", "published")


def _parse_ts(value: object) -> datetime | None:
    """Parse a STIX timestamp string into a timezone-aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = dtparser.isoparse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, OverflowError):
        return None


def _extract_timestamps(obj: dict) -> dict[str, datetime | None]:
    """Extract known timestamp fields from a STIX object."""
    return {f: _parse_ts(obj.get(f)) for f in _TIMESTAMP_FIELDS}


def _compute_per_object_diffs(
    objects_a: dict[str, dict],
    objects_b: dict[str, dict],
) -> list[dict]:
    """For objects present in both bundles, compute timestamp diffs."""
    results: list[dict] = []
    common_ids = set(objects_a) & set(objects_b)

    for stix_id in sorted(common_ids):
        ts_a = _extract_timestamps(objects_a[stix_id])
        ts_b = _extract_timestamps(objects_b[stix_id])

        diffs: list[dict] = []
        for field in _TIMESTAMP_FIELDS:
            dt_a = ts_a[field]
            dt_b = ts_b[field]
            if dt_a is None and dt_b is None:
                continue
            if dt_a is None and dt_b is not None:
                diffs.append({
                    "field": field,
                    "status": "backfilled",
                    "new_value": dt_b.isoformat(),
                    "interpretation": f"'{field}' was missing and is now set.",
                })
            elif dt_a is not None and dt_b is None:
                diffs.append({
                    "field": field,
                    "status": "removed",
                    "old_value": dt_a.isoformat(),
                    "interpretation": f"'{field}' was present and is now missing.",
                })
            elif dt_a is not None and dt_b is not None and dt_a != dt_b:
                delta = dt_b - dt_a
                direction = "forward" if delta.total_seconds() > 0 else "backward"
                total_secs = abs(delta.total_seconds())
                abs_days = int(total_secs // 86400)
                abs_hours = int((total_secs % 86400) // 3600)
                diffs.append({
                    "field": field,
                    "status": "shifted",
                    "old_value": dt_a.isoformat(),
                    "new_value": dt_b.isoformat(),
                    "delta_seconds": delta.total_seconds(),
                    "direction": direction,
                    "interpretation": (
                        f"'{field}' shifted {direction} by "
                        f"{total_secs:.0f}s "
                        f"({abs_days}d {abs_hours}h)."
                    ),
                })

        if diffs:
            results.append({
                "id": stix_id,
                "type": objects_a[stix_id].get("type"),
                "name": objects_a[stix_id].get("name"),
                "timestamp_diffs": diffs,
            })

    return results


def _windowed_trends(objects: dict[str, dict], field: str) -> dict:
    """Compute daily/weekly counts for a timestamp field across all objects."""
    daily: dict[str, int] = defaultdict(int)
    weekly: dict[str, int] = defaultdict(int)
    valid_count = 0

    for obj in objects.values():
        dt = _parse_ts(obj.get(field))
        if dt is None:
            continue
        valid_count += 1
        day_key = dt.strftime("%Y-%m-%d")
        # ISO week
        week_key = dt.strftime("%G-W%V")
        daily[day_key] += 1
        weekly[week_key] += 1

    sorted_daily = dict(sorted(daily.items()))
    sorted_weekly = dict(sorted(weekly.items()))

    # Compute acceleration (change in daily count)
    daily_values = list(sorted_daily.values())
    deltas = [daily_values[i] - daily_values[i - 1] for i in range(1, len(daily_values))]
    trend = "stable"
    if len(deltas) >= 2:
        recent = deltas[-min(3, len(deltas)):]
        avg_recent = sum(recent) / len(recent)
        if avg_recent > 0.5:
            trend = "accelerating"
        elif avg_recent < -0.5:
            trend = "decelerating"

    return {
        "field": field,
        "total_objects_with_field": valid_count,
        "daily_counts": sorted_daily,
        "weekly_counts": sorted_weekly,
        "daily_deltas": deltas,
        "trend": trend,
    }


def _single_bundle_analysis(bundle: dict) -> dict:
    """Analyze timestamps within a single bundle."""
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        return {"error": "Bundle has no 'objects' list."}

    idx: dict[str, dict] = {}
    for obj in objects:
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            idx[obj["id"]] = obj

    # Per-object timestamp coverage
    coverage: list[dict] = []
    for stix_id in sorted(idx):
        obj = idx[stix_id]
        ts = _extract_timestamps(obj)
        present = [f for f, v in ts.items() if v is not None]
        missing = [f for f, v in ts.items() if v is None]
        if present:
            coverage.append({
                "id": stix_id,
                "type": obj.get("type"),
                "name": obj.get("name"),
                "present_fields": present,
                "missing_fields": missing,
            })

    # Windowed trends for each timestamp field
    trends = {f: _windowed_trends(idx, f) for f in _TIMESTAMP_FIELDS}

    return {
        "inputs": {"object_count": len(idx)},
        "method": (
            "Timestamp extraction and trend analysis. "
            "Fields analyzed: " + ", ".join(_TIMESTAMP_FIELDS) + ". "
            "Daily/weekly bucketing with acceleration detection."
        ),
        "results": {
            "timestamp_coverage": coverage[:50],
            "timestamp_coverage_count": len(coverage),
            "trends": trends,
        },
    }


def _dual_bundle_analysis(bundle_a: dict, bundle_b: dict) -> dict:
    """Compare timestamps between two bundles."""
    idx_a: dict[str, dict] = {}
    idx_b: dict[str, dict] = {}

    for obj in (bundle_a.get("objects") or []):
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            idx_a[obj["id"]] = obj

    for obj in (bundle_b.get("objects") or []):
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            idx_b[obj["id"]] = obj

    per_object = _compute_per_object_diffs(idx_a, idx_b)

    # Drift summary
    drift_counts: dict[str, int] = defaultdict(int)
    for entry in per_object:
        for d in entry["timestamp_diffs"]:
            drift_counts[d["status"]] += 1

    return {
        "inputs": {
            "bundle_a_object_count": len(idx_a),
            "bundle_b_object_count": len(idx_b),
            "common_objects": len(set(idx_a) & set(idx_b)),
        },
        "method": (
            "Pairwise timestamp comparison for common objects. "
            "Detects shifted, backfilled, and removed timestamps. "
            "Fields analyzed: " + ", ".join(_TIMESTAMP_FIELDS) + "."
        ),
        "intermediate_values": {
            "drift_summary": dict(drift_counts),
            "objects_with_drifts": len(per_object),
        },
        "results": {
            "per_object_diffs": per_object[:50],
            "per_object_diffs_count": len(per_object),
        },
    }


def build_timeline_diff_tool() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build a STIX timeline analysis tool."""

    async def _analyze_stix_timeline(
        bundle_json: str = "",
        bundle: dict | None = None,
        comparison_bundle_json: str = "",
        comparison_bundle: dict | None = None,
    ) -> str:
        errors: list[str] = []

        def _parse(raw: str, parsed: dict | None, label: str) -> dict | None:
            if parsed is not None:
                if isinstance(parsed, dict):
                    return parsed
                errors.append(f"'{label}' must be a JSON object.")
                return None
            if raw:
                try:
                    loaded = json.loads(raw)
                    if not isinstance(loaded, dict):
                        errors.append(f"'{label}' JSON root must be an object.")
                        return None
                    return loaded
                except json.JSONDecodeError as exc:
                    errors.append(f"Invalid JSON in '{label}': {exc.msg}")
                    return None
            return None

        primary = _parse(bundle_json, bundle, "bundle")
        comparison = _parse(
            comparison_bundle_json, comparison_bundle, "comparison_bundle"
        )

        if errors:
            return json.dumps({"error": True, "errors": errors}, ensure_ascii=False)

        if primary is None:
            return json.dumps(
                {"error": True, "errors": ["Provide 'bundle_json' or 'bundle'."]},
                ensure_ascii=False,
            )

        if comparison is not None:
            result = _dual_bundle_analysis(primary, comparison)
        else:
            result = _single_bundle_analysis(primary)

        return json.dumps(
            normalize_tool_error(result), ensure_ascii=False, default=str
        )

    executor = CallableToolExecutor()
    executor.register("analyze_stix_timeline", _analyze_stix_timeline)

    definition = ToolDefinition(
        name="analyze_stix_timeline",
        description=(
            "Analyze timestamps in STIX 2.1 bundles. With one bundle: "
            "produces timestamp coverage and windowed trends (daily/weekly "
            "counts, acceleration). With two bundles: detects timeline drift "
            "(shifted, backfilled, removed dates) across common objects. "
            "Analyzes: created, modified, first_seen, last_seen, published."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bundle_json": {
                    "type": "string",
                    "description": "Primary STIX bundle as a JSON string.",
                },
                "bundle": {
                    "type": "object",
                    "description": "Primary STIX bundle as a parsed object.",
                },
                "comparison_bundle_json": {
                    "type": "string",
                    "description": "Optional second bundle JSON for comparison.",
                },
                "comparison_bundle": {
                    "type": "object",
                    "description": "Optional second bundle object for comparison.",
                },
            },
        },
        source=_FIMI_SOURCE,
    )

    return [definition], executor
