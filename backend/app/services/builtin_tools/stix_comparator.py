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

"""STIX bundle comparison and diff tool for FIMI analytics.

Compares two STIX 2.1 bundles and returns object-level, relationship-level,
and field-level diffs with duplicate/collision detection.
"""

from __future__ import annotations

import json
from collections import defaultdict

from app.tools.base import CallableToolExecutor, ToolDefinition

_FIMI_SOURCE = "builtin:cti"

# Key STIX attributes to track for field-level diffs
_DIFF_FIELDS = (
    "confidence",
    "labels",
    "external_references",
    "object_marking_refs",
    "created",
    "modified",
    "name",
    "description",
    "first_seen",
    "last_seen",
    "published",
)


def _index_objects(bundle: dict) -> dict[str, dict]:
    """Index bundle objects by STIX ID."""
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        return {}
    idx: dict[str, dict] = {}
    for obj in objects:
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            idx[obj["id"]] = obj
    return idx


def _field_diffs(old: dict, new: dict) -> list[dict]:
    """Compute field-level diffs for tracked STIX attributes."""
    diffs: list[dict] = []
    for field in _DIFF_FIELDS:
        old_val = old.get(field)
        new_val = new.get(field)
        if old_val != new_val:
            diffs.append({
                "field": field,
                "old_value": old_val,
                "new_value": new_val,
            })
    return diffs


def _detect_duplicates(
    objects_a: dict[str, dict],
    objects_b: dict[str, dict],
) -> list[dict]:
    """Detect potential duplicates: same type+name but different STIX IDs."""
    key_to_ids_a: dict[tuple[str, str], list[str]] = defaultdict(list)
    key_to_ids_b: dict[tuple[str, str], list[str]] = defaultdict(list)

    for stix_id, obj in objects_a.items():
        obj_type = obj.get("type", "")
        name = obj.get("name", "")
        # Guard non-string type/name: a non-string (e.g. list/dict) name is
        # unhashable and would crash the (type, name) dict key (#review).
        if isinstance(obj_type, str) and isinstance(name, str) and name:
            key_to_ids_a[(obj_type, name)].append(stix_id)

    for stix_id, obj in objects_b.items():
        obj_type = obj.get("type", "")
        name = obj.get("name", "")
        # Guard non-string type/name: a non-string (e.g. list/dict) name is
        # unhashable and would crash the (type, name) dict key (#review).
        if isinstance(obj_type, str) and isinstance(name, str) and name:
            key_to_ids_b[(obj_type, name)].append(stix_id)

    duplicates: list[dict] = []
    for key, ids_a in key_to_ids_a.items():
        ids_b = key_to_ids_b.get(key, [])
        if not ids_b:
            continue
        # Find IDs that appear in B but not in A — same name, different ID
        cross_dupes = [b_id for b_id in ids_b if b_id not in ids_a]
        if cross_dupes:
            duplicates.append({
                "type": key[0],
                "name": key[1],
                "ids_in_bundle_a": ids_a,
                "ids_in_bundle_b": cross_dupes,
                "reason": "Same type and name but different STIX IDs across bundles.",
            })

    return duplicates


def _compare(bundle_a: dict, bundle_b: dict) -> dict:
    """Core comparison logic."""
    idx_a = _index_objects(bundle_a)
    idx_b = _index_objects(bundle_b)

    ids_a = set(idx_a.keys())
    ids_b = set(idx_b.keys())

    added_ids = ids_b - ids_a
    removed_ids = ids_a - ids_b
    common_ids = ids_a & ids_b

    # Partition into objects vs relationships
    added_objects: list[dict] = []
    added_relationships: list[dict] = []
    for sid in sorted(added_ids):
        obj = idx_b[sid]
        entry = {"id": sid, "type": obj.get("type"), "name": obj.get("name")}
        if obj.get("type") == "relationship":
            entry["relationship_type"] = obj.get("relationship_type")
            entry["source_ref"] = obj.get("source_ref")
            entry["target_ref"] = obj.get("target_ref")
            added_relationships.append(entry)
        else:
            added_objects.append(entry)

    removed_objects: list[dict] = []
    removed_relationships: list[dict] = []
    for sid in sorted(removed_ids):
        obj = idx_a[sid]
        entry = {"id": sid, "type": obj.get("type"), "name": obj.get("name")}
        if obj.get("type") == "relationship":
            entry["relationship_type"] = obj.get("relationship_type")
            entry["source_ref"] = obj.get("source_ref")
            entry["target_ref"] = obj.get("target_ref")
            removed_relationships.append(entry)
        else:
            removed_objects.append(entry)

    modified_objects: list[dict] = []
    modified_relationships: list[dict] = []
    for sid in sorted(common_ids):
        diffs = _field_diffs(idx_a[sid], idx_b[sid])
        if not diffs:
            continue
        entry = {
            "id": sid,
            "type": idx_a[sid].get("type"),
            "name": idx_a[sid].get("name"),
            "changes": diffs,
        }
        if idx_a[sid].get("type") == "relationship":
            modified_relationships.append(entry)
        else:
            modified_objects.append(entry)

    duplicates = _detect_duplicates(idx_a, idx_b)

    return {
        "inputs": {
            "bundle_a_object_count": len(idx_a),
            "bundle_b_object_count": len(idx_b),
        },
        "method": (
            "Set-based comparison by STIX ID. "
            "Field-level diffs on: " + ", ".join(_DIFF_FIELDS) + ". "
            "Duplicate detection by (type, name) across bundles."
        ),
        "intermediate_values": {
            "ids_only_in_a": len(removed_ids),
            "ids_only_in_b": len(added_ids),
            "ids_in_both": len(common_ids),
            "ids_modified": len(modified_objects) + len(modified_relationships),
        },
        "results": {
            "objects": {
                "added": added_objects,
                "added_count": len(added_objects),
                "removed": removed_objects,
                "removed_count": len(removed_objects),
                "modified": modified_objects,
                "modified_count": len(modified_objects),
            },
            "relationships": {
                "added": added_relationships,
                "added_count": len(added_relationships),
                "removed": removed_relationships,
                "removed_count": len(removed_relationships),
                "modified": modified_relationships,
                "modified_count": len(modified_relationships),
            },
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
        },
    }


def build_stix_comparator_tool() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build a STIX bundle comparison tool."""

    async def _compare_stix_bundles(
        bundle_a_json: str = "",
        bundle_b_json: str = "",
        bundle_a: dict | None = None,
        bundle_b: dict | None = None,
    ) -> str:
        errors: list[str] = []

        def _parse(raw_json: str, parsed: dict | None, label: str) -> dict | None:
            if parsed is not None:
                if isinstance(parsed, dict):
                    return parsed
                errors.append(f"'{label}' must be a JSON object.")
                return None
            if raw_json:
                try:
                    loaded = json.loads(raw_json)
                    if not isinstance(loaded, dict):
                        errors.append(f"'{label}' JSON root must be an object.")
                        return None
                    return loaded
                except json.JSONDecodeError as exc:
                    errors.append(
                        f"Invalid JSON in '{label}': {exc.msg} "
                        f"(line {exc.lineno}, col {exc.colno})"
                    )
                    return None
            errors.append(f"Provide either '{label}_json' or '{label}'.")
            return None

        parsed_a = _parse(bundle_a_json, bundle_a, "bundle_a")
        parsed_b = _parse(bundle_b_json, bundle_b, "bundle_b")

        if errors or parsed_a is None or parsed_b is None:
            return json.dumps({"error": True, "errors": errors or ["Failed to parse bundles."]}, ensure_ascii=False)

        result = _compare(parsed_a, parsed_b)
        return json.dumps(result, ensure_ascii=False)

    executor = CallableToolExecutor()
    executor.register("compare_stix_bundles", _compare_stix_bundles)

    definition = ToolDefinition(
        name="compare_stix_bundles",
        description=(
            "Compare two STIX 2.1 bundles and return object-level, "
            "relationship-level, and field-level diffs. Also detects "
            "potential duplicates (same type and name, different IDs). "
            "Provide the bundles as JSON strings or parsed objects."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bundle_a_json": {
                    "type": "string",
                    "description": "First STIX bundle as a JSON string.",
                },
                "bundle_b_json": {
                    "type": "string",
                    "description": "Second STIX bundle as a JSON string.",
                },
                "bundle_a": {
                    "type": "object",
                    "description": "First STIX bundle as a parsed object (alternative to bundle_a_json).",
                },
                "bundle_b": {
                    "type": "object",
                    "description": "Second STIX bundle as a parsed object (alternative to bundle_b_json).",
                },
            },
        },
        source=_FIMI_SOURCE,
    )

    return [definition], executor
