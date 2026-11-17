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

"""FIMI-oriented derived metrics for STIX intelligence data.

Computes narrative propagation, source divergence, confidence drift,
and similarity scores from STIX bundles.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime

from dateutil import parser as dtparser

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import normalize_tool_error

_FIMI_SOURCE = "builtin:cti"


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = dtparser.isoparse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, OverflowError):
        return None


def _index_objects(bundle: dict) -> list[dict]:
    """Extract valid objects from a bundle."""
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        return []
    return [o for o in objects if isinstance(o, dict) and isinstance(o.get("id"), str)]


def _extract_sources(obj: dict) -> list[str]:
    """Extract source identifiers from external_references."""
    refs = obj.get("external_references") or []
    sources: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            src = ref.get("source_name")
            if isinstance(src, str) and src:
                sources.append(src)
    created_by = obj.get("created_by_ref")
    if isinstance(created_by, str) and created_by:
        sources.append(created_by)
    return sources


# ---------------------------------------------------------------------------
# Metric implementations
# ---------------------------------------------------------------------------

def _narrative_propagation(objects: list[dict], time_window_days: int) -> dict:
    """Track how narrative-type objects propagate across time windows."""
    # Identify narrative-relevant types
    narrative_types = {"report", "note", "opinion", "campaign", "grouping"}
    narratives = [o for o in objects if o.get("type") in narrative_types]

    if not narratives:
        return {
            "metric": "narrative_propagation",
            "error": "No narrative-type objects found (report, note, opinion, campaign, grouping).",
        }

    # Parse timestamps once, then anchor the windows on the EARLIEST narrative
    # so the first bucket is always window_0 (no negative indices) regardless of
    # the dataset's absolute dates.
    dated: list[tuple[datetime, str]] = []
    for obj in narratives:
        dt = _parse_ts(obj.get("created") or obj.get("published") or obj.get("modified"))
        if dt is not None:
            dated.append((dt, obj["id"]))

    if not dated:
        return {
            "metric": "narrative_propagation",
            "error": "No narrative-type objects with a parseable timestamp.",
            "intermediate_values": {"narrative_count": len(narratives)},
        }

    epoch = min(dt for dt, _ in dated)
    span = max(time_window_days, 1)
    # Key by INTEGER window index so windows sort chronologically; a string key
    # like "window_10" would sort lexicographically before "window_2".
    windows: dict[int, list[str]] = defaultdict(list)
    for dt, stix_id in dated:
        windows[(dt - epoch).days // span].append(stix_id)

    sorted_idx = sorted(windows)
    counts = [len(windows[i]) for i in sorted_idx]

    # Growth between consecutive windows
    deltas = [counts[i] - counts[i - 1] for i in range(1, len(counts))]
    propagation_rate = sum(deltas) / len(deltas) if deltas else 0.0

    return {
        "metric": "narrative_propagation",
        "method": (
            f"Count narrative-type objects per {time_window_days}-day window "
            "(anchored on the earliest narrative). Compute inter-window deltas "
            "to measure propagation acceleration."
        ),
        "intermediate_values": {
            "narrative_count": len(narratives),
            "window_count": len(sorted_idx),
            "per_window_counts": {f"window_{i}": len(windows[i]) for i in sorted_idx},
            "inter_window_deltas": deltas,
        },
        "score": round(propagation_rate, 6),
        "interpretation": (
            f"Average propagation rate = {propagation_rate:+.2f} narratives/window. "
            + ("Increasing narrative production." if propagation_rate > 0.5
               else "Decreasing narrative production." if propagation_rate < -0.5
               else "Stable narrative production.")
        ),
    }


def _source_divergence(objects: list[dict]) -> dict:
    """Measure divergence/convergence across intelligence sources."""
    source_to_ids: dict[str, set[str]] = defaultdict(set)

    for obj in objects:
        sources = _extract_sources(obj)
        obj_type = obj.get("type", "")
        obj_name = obj.get("name", obj["id"])
        entity_key = f"{obj_type}:{obj_name}"
        for src in sources:
            source_to_ids[src].add(entity_key)

    if len(source_to_ids) < 2:
        return {
            "metric": "source_divergence",
            "error": f"Need at least 2 sources, found {len(source_to_ids)}.",
            "sources_found": list(source_to_ids.keys()),
        }

    # Pairwise Jaccard between sources
    source_names = sorted(source_to_ids.keys())
    pairwise: list[dict] = []
    jaccard_values: list[float] = []

    for i in range(len(source_names)):
        for j in range(i + 1, len(source_names)):
            a_set = source_to_ids[source_names[i]]
            b_set = source_to_ids[source_names[j]]
            intersection = a_set & b_set
            union = a_set | b_set
            jaccard = len(intersection) / len(union) if union else 0.0
            jaccard_values.append(jaccard)
            pairwise.append({
                "source_a": source_names[i],
                "source_b": source_names[j],
                "overlap": round(jaccard, 4),
                "shared_entities": len(intersection),
                "union_size": len(union),
            })

    avg_overlap = sum(jaccard_values) / len(jaccard_values) if jaccard_values else 0.0

    return {
        "metric": "source_divergence",
        "method": (
            "Pairwise Jaccard similarity between entity sets per source. "
            "Low overlap = divergent sources, high overlap = convergent."
        ),
        "intermediate_values": {
            "source_count": len(source_names),
            "sources": source_names,
            "entities_per_source": {s: len(ids) for s, ids in source_to_ids.items()},
        },
        "pairwise_comparisons": pairwise,
        "score": round(avg_overlap, 6),
        "interpretation": (
            f"Average source overlap = {avg_overlap:.4f}. "
            + ("Sources are highly convergent (> 0.7)." if avg_overlap > 0.7
               else "Sources are moderately aligned." if avg_overlap > 0.3
               else "Sources are divergent (< 0.3) — covering different entities.")
        ),
    }


def _coerce_confidence(value: object) -> float | None:
    """Coerce a confidence value to float, or None if not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence_drift(
    objects: list[dict],
    objects_b: list[dict] | None,
    anomaly_threshold: float = 30.0,
) -> dict:
    """Detect objects with unusual confidence changes.

    ``anomaly_threshold`` is an absolute confidence delta on the STIX 2.1
    confidence scale (0-100); a snapshot-to-snapshot change with
    ``|Δ| > anomaly_threshold`` is flagged as an anomaly. Callers using a
    different scale (e.g. 0-1) should pass a matching threshold.
    """
    if objects_b is not None:
        # Compare confidence between two snapshots
        conf_a: dict[str, float | None] = {}
        conf_b: dict[str, float | None] = {}
        index_a: dict[str, dict] = {}

        for obj in objects:
            conf_a[obj["id"]] = _coerce_confidence(obj.get("confidence"))
            index_a[obj["id"]] = obj
        for obj in objects_b:
            conf_b[obj["id"]] = _coerce_confidence(obj.get("confidence"))

        drifts: list[dict] = []
        for stix_id in sorted(set(conf_a) & set(conf_b)):
            ca = conf_a[stix_id]
            cb = conf_b[stix_id]
            if ca is None and cb is None:
                continue
            if ca != cb:
                delta = (cb or 0.0) - (ca or 0.0)
                obj_a = index_a.get(stix_id, {})
                drifts.append({
                    "id": stix_id,
                    "type": obj_a.get("type"),
                    "name": obj_a.get("name"),
                    "old_confidence": ca,
                    "new_confidence": cb,
                    "delta": delta,
                    "anomaly": abs(delta) > anomaly_threshold,
                })

        anomaly_count = sum(1 for d in drifts if d.get("anomaly"))
        return {
            "metric": "confidence_drift",
            "method": (
                "Pairwise confidence comparison across two snapshots. "
                f"Anomaly threshold: |Δ| > {anomaly_threshold} "
                "(absolute change on the STIX 0-100 confidence scale)."
            ),
            "intermediate_values": {
                "compared_objects": len(set(conf_a) & set(conf_b)),
                "objects_with_drift": len(drifts),
                "anomaly_threshold": anomaly_threshold,
            },
            "drifts": drifts[:50],
            "drift_count": len(drifts),
            "anomaly_count": anomaly_count,
            "interpretation": (
                f"{len(drifts)} objects with confidence changes, "
                f"{anomaly_count} flagged as anomalies (|Δ| > {anomaly_threshold})."
            ),
        }
    else:
        # Single-bundle: flag objects with extreme or missing confidence
        flagged: list[dict] = []
        confidence_values: list[float] = []
        for obj in objects:
            conf = obj.get("confidence")
            if conf is not None and isinstance(conf, (int, float)):
                confidence_values.append(float(conf))

        mean_conf = sum(confidence_values) / len(confidence_values) if confidence_values else 0
        std_conf = (
            math.sqrt(sum((x - mean_conf) ** 2 for x in confidence_values) / len(confidence_values))
            if len(confidence_values) > 1 else 0
        )

        for obj in objects:
            conf = obj.get("confidence")
            if conf is None:
                flagged.append({
                    "id": obj["id"],
                    "type": obj.get("type"),
                    "name": obj.get("name"),
                    "confidence": None,
                    "flag": "missing",
                })
            elif isinstance(conf, (int, float)) and std_conf > 0:
                z_score = (conf - mean_conf) / std_conf
                if abs(z_score) > 2:
                    flagged.append({
                        "id": obj["id"],
                        "type": obj.get("type"),
                        "name": obj.get("name"),
                        "confidence": conf,
                        "z_score": round(z_score, 4),
                        "flag": "outlier",
                    })

        return {
            "metric": "confidence_drift",
            "method": (
                "Single-bundle analysis. Flags missing confidence and "
                "statistical outliers (|z| > 2)."
            ),
            "intermediate_values": {
                "total_objects": len(objects),
                "objects_with_confidence": len(confidence_values),
                "mean_confidence": round(mean_conf, 4),
                "std_confidence": round(std_conf, 4),
            },
            "flagged": flagged[:50],
            "flagged_count": len(flagged),
            "interpretation": (
                f"{len(flagged)} objects flagged "
                f"(mean confidence = {mean_conf:.1f}, σ = {std_conf:.1f})."
            ),
        }


def _similarity_score(objects_a: list[dict], objects_b: list[dict]) -> dict:
    """Compute Jaccard and attribute-level similarity between two snapshots."""
    # Entity-level Jaccard by STIX ID
    ids_a = {o["id"] for o in objects_a}
    ids_b = {o["id"] for o in objects_b}
    intersection = ids_a & ids_b
    union = ids_a | ids_b
    id_jaccard = len(intersection) / len(union) if union else 0.0

    # Type distribution similarity (cosine)
    types_a: dict[str, int] = defaultdict(int)
    types_b: dict[str, int] = defaultdict(int)
    for o in objects_a:
        types_a[o.get("type", "")] += 1
    for o in objects_b:
        types_b[o.get("type", "")] += 1

    all_types = set(types_a) | set(types_b)
    dot = sum(types_a.get(t, 0) * types_b.get(t, 0) for t in all_types)
    mag_a = math.sqrt(sum(v ** 2 for v in types_a.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in types_b.values()))
    type_cosine = dot / (mag_a * mag_b) if mag_a > 0 and mag_b > 0 else 0.0

    # Combined score (equal weight)
    combined = (id_jaccard + type_cosine) / 2

    return {
        "metric": "similarity_score",
        "method": (
            "Combined similarity: Jaccard on STIX IDs (entity overlap) + "
            "cosine similarity on type distributions. Equal weighting."
        ),
        "intermediate_values": {
            "bundle_a_size": len(ids_a),
            "bundle_b_size": len(ids_b),
            "shared_ids": len(intersection),
            "type_distribution_a": dict(types_a),
            "type_distribution_b": dict(types_b),
        },
        "scores": {
            "id_jaccard": round(id_jaccard, 6),
            "type_cosine": round(type_cosine, 6),
            "combined": round(combined, 6),
        },
        "score": round(combined, 6),
        "interpretation": (
            f"Similarity = {combined:.4f} "
            f"(ID overlap: {id_jaccard:.4f}, type distribution: {type_cosine:.4f}). "
            + ("Very similar snapshots." if combined > 0.8
               else "Moderately similar." if combined > 0.4
               else "Substantially different snapshots.")
        ),
    }


_METRIC_TYPES = {
    "narrative_propagation": "Track narrative spread across time windows",
    "source_divergence": "Compare entity coverage across intelligence sources",
    "confidence_drift": "Flag objects with unusual confidence changes",
    "similarity_score": "Jaccard + cosine similarity between two snapshots",
}


def build_fimi_metrics_tool() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build FIMI derived metrics tool."""

    async def _compute_fimi_derived_metrics(
        metric: str = "",
        bundle_json: str = "",
        bundle: dict | None = None,
        comparison_bundle_json: str = "",
        comparison_bundle: dict | None = None,
        time_window_days: int = 7,
        anomaly_threshold: float = 30.0,
    ) -> str:
        errors: list[str] = []

        if not metric:
            return json.dumps({
                "error": True,
                "message": "Provide a 'metric' parameter.",
                "available_metrics": _METRIC_TYPES,
            })

        if metric not in _METRIC_TYPES:
            return json.dumps({
                "error": True,
                "message": f"Unknown metric '{metric}'.",
                "available_metrics": _METRIC_TYPES,
            })

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
        comparison = _parse(comparison_bundle_json, comparison_bundle, "comparison_bundle")

        if errors:
            return json.dumps({"error": True, "errors": errors}, ensure_ascii=False)

        if primary is None:
            return json.dumps(
                {"error": True, "errors": ["Provide 'bundle_json' or 'bundle'."]},
                ensure_ascii=False,
            )

        objects_a = _index_objects(primary)
        objects_b = _index_objects(comparison) if comparison else None

        if metric == "narrative_propagation":
            result = _narrative_propagation(objects_a, time_window_days)
        elif metric == "source_divergence":
            result = _source_divergence(objects_a)
        elif metric == "confidence_drift":
            result = _confidence_drift(objects_a, objects_b, anomaly_threshold)
        elif metric == "similarity_score":
            if objects_b is None:
                return json.dumps({
                    "error": True,
                    "message": "similarity_score requires two bundles.",
                })
            result = _similarity_score(objects_a, objects_b)
        else:
            return json.dumps({"error": True, "message": f"Unimplemented: {metric}"})

        return json.dumps(
            normalize_tool_error(result), ensure_ascii=False, default=str
        )

    executor = CallableToolExecutor()
    executor.register("compute_fimi_derived_metrics", _compute_fimi_derived_metrics)

    definition = ToolDefinition(
        name="compute_fimi_derived_metrics",
        description=(
            "Compute FIMI-oriented derived metrics from STIX bundles. "
            "Metrics: narrative_propagation (spread over time), "
            "source_divergence (coverage across sources), "
            "confidence_drift (unusual confidence changes, with a configurable "
            "anomaly_threshold on the STIX 0-100 confidence scale), "
            "similarity_score (Jaccard + cosine between snapshots). "
            "All outputs include method, intermediate values, and interpretation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": list(_METRIC_TYPES.keys()),
                    "description": "Which derived metric to compute.",
                },
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
                    "description": "Optional second bundle JSON (for confidence_drift, similarity_score).",
                },
                "comparison_bundle": {
                    "type": "object",
                    "description": "Optional second bundle object (for confidence_drift, similarity_score).",
                },
                "time_window_days": {
                    "type": "integer",
                    "description": "Window size in days (for narrative_propagation). Default: 7.",
                    "default": 7,
                },
                "anomaly_threshold": {
                    "type": "number",
                    "description": (
                        "Absolute confidence delta that flags an anomaly "
                        "(for confidence_drift), on the STIX 0-100 confidence "
                        "scale. Default: 30. Pass a matching value if your data "
                        "uses a different scale (e.g. 0.3 for a 0-1 scale)."
                    ),
                    "default": 30,
                },
            },
            "required": ["metric"],
        },
        source=_FIMI_SOURCE,
    )

    return [definition], executor
