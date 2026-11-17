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

"""STIX bundle validator tool and STIX id repair helpers.

The validator normalises TLP markings to their STIX 2.1 spec form, repairs the
object ids that are malformed (never the ones that are already valid), runs
python-stix2 over the result, and guards against an unwinnable fix-and-retry
loop by going terminal on a repeated identical error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from copy import deepcopy
from typing import Any

from app.core.log_once import fingerprint, log_once_soon
from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult
from app.tools.json_patch import JsonPatchError, apply_json_patch

from ._shared import _bounded_display, _is_uuid_like

logger = logging.getLogger(__name__)

#: How often the standard-id fallback is said, per cause, across the platform.
_STANDARD_ID_FALLBACK_NOTICE_INTERVAL_SECONDS = 3600

_STIX_DETERMINISTIC_NAMESPACE = uuid.UUID("6f29f8b2-6830-4e4f-9fb0-2f5a56a95c9d")

# Consecutive identical validation failures after which the validator stops
# asking the model to try again (see the repeat guard in the tool below).
_REPEAT_GUARD_THRESHOLD = 3

# A validated bundle longer than this (compact JSON characters) is not echoed
# back to the model: the result names it by ``$candidate`` instead, and the
# whole result rides on ``ToolResult.structured`` for the finalize fast path.
# A large input re-sent on every failed validation outgrew the model window.
_INLINE_BUNDLE_MAX_CHARS = 20_000

#: How the validator names the bundle it validated last when it does not echo
#: it: a call with ``patch_candidate`` true applies its patch to that bundle.
CANDIDATE_REFERENCE = "$candidate"


def _is_valid_stix_id(value: object, expected_type: str | None = None) -> bool:
    if not isinstance(value, str) or "--" not in value:
        return False
    obj_type, obj_uuid = value.split("--", 1)
    if expected_type and obj_type != expected_type:
        return False
    return _is_uuid_like(obj_uuid)


def _normalize_for_deterministic_id(node: object, *, key_hint: str | None = None) -> object:
    """Canonicalize STIX object payload for deterministic UUID fallback.

    - Drops ``id`` so caller-provided unstable IDs never affect the rewrite.
    - Normalizes ``*_ref`` / ``*_refs`` STIX references to their object type only
      (e.g. ``campaign--abc`` -> ``campaign--<ref>``), preventing UUID noise.
    """
    if isinstance(node, dict):
        normalized: dict[str, object] = {}
        for key in sorted(node.keys()):
            if key == "id":
                continue
            normalized[key] = _normalize_for_deterministic_id(node[key], key_hint=key)
        return normalized

    if isinstance(node, list):
        return [_normalize_for_deterministic_id(item, key_hint=key_hint) for item in node]

    if isinstance(node, str) and key_hint:
        if key_hint.endswith("_ref") and "--" in node:
            ref_type, _ = node.split("--", 1)
            return f"{ref_type}--<ref>"
        if key_hint.endswith("_refs") and "--" in node:
            ref_type, _ = node.split("--", 1)
            return f"{ref_type}--<ref>"

    return node


def _deterministic_uuid_fallback(stix_type: str, data: dict) -> str:
    normalized = _normalize_for_deterministic_id(data)
    seed = json.dumps(
        {"type": stix_type, "data": normalized},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return str(uuid.uuid5(_STIX_DETERMINISTIC_NAMESPACE, seed))


# ---------------------------------------------------------------------------
# pycti standard ID generation — uses the official pycti client library
# ---------------------------------------------------------------------------
_PYCTI_STIX2_HELPER = None
#: Why pycti cannot be imported in this process, once it was tried: the import is
#: heavy, and a bundle repairs its objects one by one.
_PYCTI_IMPORT_FAILURE: tuple[type[ImportError], str] | None = None


def _get_pycti_stix2_helper():
    """Lazily create a pycti stix2 helper (no network required)."""
    global _PYCTI_STIX2_HELPER, _PYCTI_IMPORT_FAILURE
    if _PYCTI_STIX2_HELPER is None:
        if _PYCTI_IMPORT_FAILURE is not None:
            failure, message = _PYCTI_IMPORT_FAILURE
            raise failure(message)
        try:
            from pycti import OpenCTIApiClient
        except ImportError as exc:
            _PYCTI_IMPORT_FAILURE = (type(exc), str(exc))
            raise

        client = OpenCTIApiClient(
            "http://localhost:0", "dummy", perform_health_check=False
        )
        _PYCTI_STIX2_HELPER = client.stix2
    return _PYCTI_STIX2_HELPER


def _generate_standard_id_from_stix(data: dict) -> str | None:
    """Generate a deterministic STIX id using pycti's official implementation."""
    stix_type = data.get("type")
    if not isinstance(stix_type, str) or not stix_type:
        return None

    try:
        return _get_pycti_stix2_helper().generate_standard_id_from_stix(data)
    except Exception as exc:
        # Deterministic fallback in case pycti cannot derive the ID for this
        # object, or cannot load at all (without libmagic). The id is then
        # not the standard one other STIX tools derive, so it is said, once per cause
        # (the exception and its message).
        log_once_soon(
            logger,
            logging.WARNING,
            f"stix_standard_id_fallback:{fingerprint(type(exc).__name__, str(exc)[:200])}",
            "pycti could not give a %s its STIX standard id (%s: %s): it gets a deterministic "
            "id of its own, which a platform matching objects by standard id will not recognise.",
            stix_type,
            type(exc).__name__,
            format_exception_message(exc),
            interval=_STANDARD_ID_FALLBACK_NOTICE_INTERVAL_SECONDS,
        )
        return f"{stix_type}--{_deterministic_uuid_fallback(stix_type, data)}"


def _rewrite_refs_with_remap(node: object, id_remap: dict[str, str]) -> object:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key.endswith("_ref") and isinstance(value, str):
                node[key] = id_remap.get(value, value)
            elif key.endswith("_refs") and isinstance(value, list):
                node[key] = [id_remap.get(v, v) if isinstance(v, str) else v for v in value]
            else:
                node[key] = _rewrite_refs_with_remap(value, id_remap)
        return node
    if isinstance(node, list):
        for idx, item in enumerate(node):
            node[idx] = _rewrite_refs_with_remap(item, id_remap)
        return node
    return node


def _fix_bundle_uuid_errors(bundle: dict) -> tuple[dict, bool, list[str]]:
    """Fix invalid STIX IDs in a bundle.

    Deprecated: a thin wrapper over :func:`_rewrite_bundle_uuids`, which does
    exactly this job.  No live caller — kept for one release for its historical
    call signature and the facade re-export.

    Returns:
        (fixed_bundle, changed, warnings)
    """
    fixed, warnings = _rewrite_bundle_uuids(bundle)
    return fixed, bool(warnings), warnings


# ── TLP markings ────────────────────────────────────────────────────────────
#
# STIX 2.1 freezes both the id and the ``created`` date of the four TLP
# markings (see ``stix2/markings/utils.py::check_tlp_marking``): python-stix2
# refuses a ``tlp`` marking-definition carrying anything else, whatever the
# rest of the object looks like.  Those ids are identity, not payload — they
# must survive this module untouched.
_TLP_SPEC_IDS: dict[str, str] = {
    "white": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    "green": "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    "amber": "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    "red": "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}
_TLP_SPEC_CREATED = "2017-01-20T00:00:00.000Z"

# TLP 2.0 renamed WHITE to CLEAR without changing its meaning, and OpenCTI
# ships TLP:CLEAR on the very id STIX 2.1 froze for TLP:WHITE.  STIX 2.1 knows
# no ``clear`` value, so it has to be emitted as the spec white marking or
# python-stix2 rejects an otherwise perfectly legitimate OpenCTI bundle.
_TLP_COLOR_ALIASES: dict[str, str] = {"clear": "white"}


def _compact_json(value: object, budget: int = 160) -> str:
    """Render a value compactly for a warning message, bounded in length.

    Only ever called on a ``definition`` body, which the caller already sent us
    as parsed JSON, so the dump is bounded by the incoming bundle.
    """
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return _bounded_display(value)
    return rendered if len(rendered) <= budget else rendered[:budget] + "…"


def _tlp_color(obj: object) -> str | None:
    """Return the canonical spec TLP colour of a marking-definition, if any.

    Accepts every spelling seen in the wild — ``definition_type`` as ``tlp`` or
    ``TLP``, ``definition`` as ``{"tlp": "clear"}`` or ``{"TLP": "TLP:CLEAR"}``,
    and a bare ``name: "TLP:CLEAR"`` — and maps aliases (``clear`` -> ``white``).
    Returns *None* for anything that is not one of the four spec colours.
    """
    if not isinstance(obj, dict) or obj.get("type") != "marking-definition":
        return None

    definition_type = obj.get("definition_type")
    is_tlp_type = isinstance(definition_type, str) and definition_type.strip().lower() == "tlp"

    raw: str | None = None
    definition = obj.get("definition")
    if isinstance(definition, dict):
        for key, value in definition.items():
            if isinstance(key, str) and key.strip().lower() == "tlp" and isinstance(value, str):
                raw = value
                break
        if raw is None and not is_tlp_type:
            # A non-tlp definition body (a statement marking, say) is not a TLP
            # marking however its ``name`` reads — rewriting it would clobber it.
            return None
    # A ``tlp`` definition body identifies the marking on its own, even when
    # ``definition_type`` is missing or misspelled (which normalization fixes).
    from_tlp_definition = raw is not None

    name = obj.get("name")
    if raw is None and isinstance(name, str):
        raw = name

    if raw is None:
        return None
    if not is_tlp_type and not from_tlp_definition and not raw.strip().lower().startswith("tlp"):
        # Falling back to ``name`` only: it has to read like a TLP marking.
        return None

    color = raw.strip().lower().removeprefix("tlp:").strip()
    color = _TLP_COLOR_ALIASES.get(color, color)
    return color if color in _TLP_SPEC_IDS else None


def _normalize_tlp_markings(bundle: dict) -> tuple[dict, list[str]]:
    """Pin every TLP marking-definition to its STIX 2.1 spec form.

    Sets the spec id, ``definition_type: "tlp"``, ``definition: {"tlp": <colour>}``
    and the spec ``created`` date, remapping every reference to the old id.
    OpenCTI's TLP:CLEAR is emitted as the spec white marking (its ``name`` is
    left as-is, python-stix2 does not check it).  Markings whose colour is not
    one of the four spec colours are left strictly untouched — inventing a
    mapping for them would silently relabel someone's confidentiality marking.

    Returns:
        (normalized_bundle, warnings)
    """
    normalized = deepcopy(bundle)
    warnings: list[str] = []

    objects = normalized.get("objects")
    if not isinstance(objects, list):
        return normalized, warnings

    id_remap: dict[str, str] = {}
    seen_spec_ids: set[str] = set()
    kept: list[object] = []

    for idx, obj in enumerate(objects):
        if not isinstance(obj, dict):
            kept.append(obj)
            continue
        color = _tlp_color(obj)
        if color is None:
            kept.append(obj)
            continue

        spec_id = _TLP_SPEC_IDS[color]
        old_id = obj.get("id")
        changes: list[str] = []

        if old_id != spec_id:
            if isinstance(old_id, str) and old_id:
                id_remap[old_id] = spec_id
            obj["id"] = spec_id
            changes.append(f"id '{old_id or '(missing)'}' -> '{spec_id}'")
        if obj.get("definition_type") != "tlp":
            obj["definition_type"] = "tlp"
            changes.append("definition_type -> 'tlp'")
        old_definition = obj.get("definition")
        if old_definition != {"tlp": color}:
            obj["definition"] = {"tlp": color}
            # Show what was replaced: a marking declared ``tlp`` but carrying a
            # foreign body (a statement, say) loses it here, and a warning that
            # only prints the new value would hide that.
            changes.append(
                f"definition {_compact_json(old_definition)} -> {{'tlp': '{color}'}}"
            )
        if obj.get("created") != _TLP_SPEC_CREATED:
            obj["created"] = _TLP_SPEC_CREATED
            changes.append(f"created -> '{_TLP_SPEC_CREATED}'")

        if spec_id in seen_spec_ids:
            # Two spellings of the same colour (e.g. TLP:CLEAR and TLP:WHITE)
            # collapse onto one spec id; keeping both would duplicate an id.
            warnings.append(f"objects[{idx}]: dropped duplicate TLP:{color.upper()} marking-definition.")
            continue

        seen_spec_ids.add(spec_id)
        kept.append(obj)
        if changes:
            warnings.append(f"objects[{idx}]: normalized TLP:{color.upper()} marking ({', '.join(changes)}).")

    normalized["objects"] = kept
    if id_remap:
        _rewrite_refs_with_remap(normalized, id_remap)

    return normalized, warnings


def _rewrite_bundle_uuids(bundle: dict) -> tuple[dict, list[str]]:
    """Repair *invalid* STIX ids in a bundle, leaving valid ids untouched.

    An id the model got right is part of the object's identity in both STIX and
    OpenCTI, and some ids are pinned by the spec itself (the four TLP markings),
    so renumbering everything made those objects unvalidatable — the model could
    not fix by editing what the validator broke on every call.  Only
    missing or malformed ids are regenerated, deterministically, from content.
    All ``*_ref`` / ``*_refs`` fields are remapped to the repaired ids.

    Returns:
        (repaired_bundle, warnings) — warnings list each repaired ID.
    """
    repaired = deepcopy(bundle)
    warnings: list[str] = []

    # Ensure bundle has a valid top-level id
    bundle_id = repaired.get("id")
    if not _is_valid_stix_id(bundle_id, "bundle"):
        repaired["id"] = f"bundle--{uuid.uuid4()}"
        warnings.append("bundle: generated missing bundle id.")

    objects = repaired.get("objects")
    if not isinstance(objects, list):
        return repaired, warnings

    id_remap: dict[str, str] = {}

    # Pass 1: regenerate the ids that are missing or malformed
    for idx, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type")
        if not isinstance(obj_type, str) or not obj_type:
            continue
        old_id = obj.get("id")
        if _is_valid_stix_id(old_id, obj_type):
            continue
        new_id = _generate_standard_id_from_stix(obj)
        if not new_id:
            # Deterministic fallback: keep the repair stable across executions.
            new_id = f"{obj_type}--{_deterministic_uuid_fallback(obj_type, obj)}"
        if old_id != new_id:
            if isinstance(old_id, str) and old_id:
                id_remap[old_id] = new_id
            obj["id"] = new_id
            warnings.append(
                f"objects[{idx}] ({obj_type}): repaired invalid id "
                f"'{old_id or '(missing)'}' → '{new_id}'."
            )

    # Pass 2: remap all _ref and _refs fields
    if id_remap:
        _rewrite_refs_with_remap(repaired, id_remap)

    return repaired, warnings


# ── References and input comparison ────────────────────────────────────────

# Past this many findings of one kind the list ends with a count, so a bundle
# with thousands of dangling refs cannot flood the model's context.
_MAX_LISTED_FINDINGS = 50

# The four spec TLP markings are known to every STIX consumer (OpenCTI ships
# them), so a bundle may reference them without carrying their definition.
_WELL_KNOWN_REFS = frozenset(_TLP_SPEC_IDS.values())

# The list indexes of a ref's field path (object_refs[3] -> object_refs).
_LIST_INDEX = re.compile(r"\[\d+\]")


def _iter_refs(node: object, path: str):
    """Yield ``(field_path, ref)`` for every ``*_ref`` / ``*_refs`` id under *node*."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            field = f"{path}.{key}" if path else key
            if key.endswith("_ref") and isinstance(value, str):
                yield field, value
            elif key.endswith("_refs") and isinstance(value, list):
                for i, ref in enumerate(value):
                    if isinstance(ref, str):
                        yield f"{field}[{i}]", ref
            else:
                yield from _iter_refs(value, field)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _iter_refs(item, f"{path}[{i}]")


def _capped(findings: list[str], what: str) -> list[str]:
    if len(findings) <= _MAX_LISTED_FINDINGS:
        return findings
    rest = len(findings) - _MAX_LISTED_FINDINGS
    return [*findings[:_MAX_LISTED_FINDINGS], f"... and {rest} more {what}."]


def _dangling_refs(bundle: dict) -> list[tuple[tuple[object, str, str], str]]:
    """List every ref of the bundle that points to no object of the bundle.

    Each entry is ``(key, message)``: the key ``(object id, field without list
    indexes, ref)`` names the dangling ref whatever its position in a list, so
    a ref inserted before it does not make it look new.  A ref that is not a
    STIX id (no ``--``) is left to python-stix2, which already refuses it; the
    spec TLP markings are never reported.
    """
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        return []
    known = {obj.get("id") for obj in objects if isinstance(obj, dict)}
    findings: list[tuple[tuple[object, str, str], str]] = []
    for idx, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type") or "?"
        for field, ref in _iter_refs(obj, ""):
            if "--" not in ref or ref in known or ref in _WELL_KNOWN_REFS:
                continue
            key = (obj.get("id"), _LIST_INDEX.sub("", field), ref)
            findings.append((key, f"objects[{idx}] ({obj_type}) {field}: '{ref}' is not an object of this bundle."))
    return findings


def _unresolved_refs(bundle: dict) -> list[str]:
    """Name every ref of the bundle that points to no object of the bundle."""
    return [message for _, message in _dangling_refs(bundle)]


def _split_dangling_against_input(candidate: dict, original: dict) -> tuple[list[str], list[str]]:
    """Split the candidate's dangling refs into ``(introduced, inherited)``.

    A ref already dangling at the same object and field of the input bundle is
    inherited: a lossless transformer must keep it, so it is only reported.
    Any other dangling ref was introduced or newly broken by the agent.
    """
    inherited_keys = {key for key, _ in _dangling_refs(original)}
    introduced: list[str] = []
    inherited: list[str] = []
    for key, message in _dangling_refs(candidate):
        if key in inherited_keys:
            inherited.append(f"{message[:-1]}: already dangling in input_bundle.")
        else:
            introduced.append(message)
    return introduced, inherited


def _object_spec_errors(stix2_module: Any, bundle: dict[str, Any]) -> dict[int, tuple[object, str]]:
    """Parse every object of the bundle alone: ``{index: (id, stix2 error)}`` of those that fail."""
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        return {}
    failures: dict[int, tuple[object, str]] = {}
    for idx, obj in enumerate(objects):
        try:
            stix2_module.parse(obj, allow_custom=True, version="2.1")
        except Exception as exc:
            obj_id = obj.get("id") if isinstance(obj, dict) else None
            failures[idx] = (obj_id, str(exc))
    return failures


def _split_spec_errors_against_input(
    stix2_module: Any, candidate: dict[str, Any], original: dict[str, Any]
) -> tuple[list[str], list[str]] | None:
    """Split the candidate's stix2 spec errors into ``(introduced, inherited)``.

    Each object is parsed alone, so an inherited defect never masks an
    introduced one.  A failing object whose input object (same id) fails with
    the same error is inherited: a lossless transformer must keep it, so it is
    only reported.  Any other failure was introduced by the agent.  ``None``
    when no object fails alone: the defect is the bundle's own.
    """
    failures = _object_spec_errors(stix2_module, candidate)
    if not failures:
        return None
    input_failures = {
        obj_id: message
        for obj_id, message in _object_spec_errors(stix2_module, original).values()
        if isinstance(obj_id, str)
    }
    objects = candidate.get("objects")
    introduced: list[str] = []
    inherited: list[str] = []
    for idx, (obj_id, message) in failures.items():
        obj = objects[idx] if isinstance(objects, list) else None
        obj_type = (obj.get("type") if isinstance(obj, dict) else None) or "?"
        finding = f"stix2 validation error: objects[{idx}] ({obj_type}) '{obj_id}': {message}"
        if isinstance(obj_id, str) and input_failures.get(obj_id) == message:
            inherited.append(f"{finding.rstrip('.')}: already present in input_bundle.")
        else:
            introduced.append(finding)
    # The bundle without its failing objects must still parse: a bundle-level
    # defect is never inherited.
    rest = {key: value for key, value in candidate.items() if key != "objects"}
    kept = [obj for idx, obj in enumerate(objects) if idx not in failures] if isinstance(objects, list) else []
    if kept:
        rest["objects"] = kept
    try:
        stix2_module.parse(rest, allow_custom=True, version="2.1")
    except Exception as exc:
        introduced.append(f"stix2 validation error: {exc}")
    return introduced, inherited


def _lost_against_input(candidate: dict, original: dict) -> list[str]:
    """Name the input objects, and the properties of kept objects, the candidate lost."""
    in_objects = original.get("objects")
    out_objects = candidate.get("objects")
    if not isinstance(in_objects, list):
        return []
    # A candidate whose objects is not an array kept none of them: every input
    # object is lost, never "nothing compared".
    if not isinstance(out_objects, list):
        out_objects = []
    out_by_id = {
        obj.get("id"): (idx, obj)
        for idx, obj in enumerate(out_objects)
        if isinstance(obj, dict) and isinstance(obj.get("id"), str)
    }
    findings: list[str] = []
    for obj in in_objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            continue
        obj_id = obj["id"]
        kept = out_by_id.get(obj_id)
        if kept is None:
            findings.append(f"input object '{obj_id}' ({obj.get('type') or '?'}) is not in the bundle.")
            continue
        idx, out = kept
        for prop in obj:
            if prop not in out:
                findings.append(
                    f"objects[{idx}] ({obj.get('type') or '?'}): property '{prop}' of input "
                    f"object '{obj_id}' is missing."
                )
    return findings


# A changed value longer than this is named by its length and hash, never quoted.
_MAX_QUOTED_VALUE_CHARS = 80


def _describe_value(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= _MAX_QUOTED_VALUE_CHARS:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"<{len(text)} chars, sha256 {digest}>"


def _changed_against_input(candidate: dict[str, object], original: dict[str, object]) -> list[str]:
    """Name every property of a kept input object whose value the candidate changed.

    A transformer regenerating the whole bundle can alter a value it was never
    asked to touch (a report renamed); lost objects and properties are already
    reported, this names the silent rewrites.
    """
    in_objects = original.get("objects")
    out_objects = candidate.get("objects")
    if not isinstance(in_objects, list) or not isinstance(out_objects, list):
        return []
    out_by_id = {
        obj.get("id"): (idx, obj)
        for idx, obj in enumerate(out_objects)
        if isinstance(obj, dict) and isinstance(obj.get("id"), str)
    }
    findings: list[str] = []
    for obj in in_objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            continue
        kept = out_by_id.get(obj["id"])
        if kept is None:
            continue
        idx, out = kept
        for prop, before in obj.items():
            if prop in out and out[prop] != before:
                findings.append(
                    f"objects[{idx}] ({obj.get('type') or '?'}): property '{prop}' of input object "
                    f"'{obj['id']}' changed: {_describe_value(before)} -> {_describe_value(out[prop])}."
                )
    return findings


def _objects_kind(bundle: dict) -> str:
    if "objects" not in bundle:
        return "missing"
    value = bundle["objects"]
    if value is None:
        return "null"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, str):
        return "a string"
    return f"a {type(value).__name__}"


def _objects_shape_findings(candidate: dict, original: dict | None) -> tuple[list[str], list[str]]:
    """Errors and warnings on the bundle's ``objects`` member itself.

    Every other check walks ``objects`` as an array and finds nothing to say
    when it is not one, so a bundle whose objects were set to null or emptied
    used to validate.  Such a bundle is an error, unless the input already had
    the same shape (a defect the agent did not introduce, a warning).
    """
    hint = (
        "Patch individual objects (/objects/INDEX/PROPERTY, /objects/-), "
        "never /objects itself; to drop an object, remove /objects/INDEX."
    )
    out_kind = _objects_kind(candidate)
    in_kind = _objects_kind(original) if original is not None else None
    in_objects = original.get("objects") if original is not None else None
    in_count = len(in_objects) if isinstance(in_objects, list) else 0
    if out_kind != "an array":
        message = f"'objects' is {out_kind}, not an array of STIX objects"
        if in_kind == out_kind:
            return [], [f"{message}; input_bundle has the same defect."]
        if in_count:
            message += f": objects lost, the input had {in_count} objects, the result has 0"
        return [f"{message}. {hint}"], []
    if in_count and not candidate["objects"]:
        return [f"objects lost: the input had {in_count} objects, the result has 0. {hint}"], []
    return [], []


def _as_bundle(value: object) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def stix_validator_strict_refs(agent: object) -> bool:
    """Whether *agent* finalizes on the STIX validator, so its bundle must stand alone.

    True when ``config.finalize_on_builtin_tool`` or a builtin
    ``finalize_tool_ref`` names ``stix_bundle_validator``: the validated bundle
    is then returned as the answer, and a ref to an absent object is an error.
    """
    config = getattr(agent, "config", None)
    fast_path = config.get("finalize_on_builtin_tool") if isinstance(config, dict) else None
    if isinstance(fast_path, dict) and fast_path.get("tool_name") == "stix_bundle_validator":
        # A transformer may finalize on a bundle that still refs objects the
        # platform already holds: its config opts out with strict_refs false.
        return fast_path.get("strict_refs") is not False
    ref = getattr(agent, "finalize_tool_ref", None)
    return isinstance(ref, dict) and ref.get("source") == "builtin" and ref.get("tool_name") == "stix_bundle_validator"


# ── STIX Bundle Validator ───────────────────────────────────────────────────


def _answer(result: dict[str, object], *, inlined: bool = True, bundle_chars: int = 0) -> ToolResult:
    """The validator's answer: *result* whole on ``structured``, the model's view as text.

    The text carries the bundle only when *inlined*; otherwise it names it by
    :data:`CANDIDATE_REFERENCE` with its size, so a large bundle never travels
    back through the model's context. The finalize fast path of the agent loop
    reads ``structured``, so a valid bundle still becomes the answer whole.
    """
    shown = result
    if not inlined and "bundle" in result:
        shown = {key: value for key, value in result.items() if key != "bundle"}
        shown["bundle_ref"] = CANDIDATE_REFERENCE
        shown["bundle_chars"] = bundle_chars
        shown["bundle_note"] = (
            f"The bundle ({bundle_chars} characters) is not repeated here. Patch it with "
            "patch_candidate true and a patch (input_bundle stays your input), or patch your input again."
        )
    return ToolResult(tool_call_id="", content=json.dumps(shown, ensure_ascii=False), structured=result)


def build_stix_bundle_validator_tool(
    *, strict_refs: bool = False, inline_bundle_max_chars: int | None = _INLINE_BUNDLE_MAX_CHARS
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build a STIX 2.1 bundle validation tool.

    The returned executor keeps per-run state for the repeat guard, so a fresh
    builder call is one agent run's worth of validation history.

    *strict_refs* makes a ref to an object absent from the bundle an error
    (``valid: false``) instead of a warning.  It is on when the validator is
    the agent's finalize step: there the bundle is the answer and must stand
    alone.  Elsewhere a ref may legitimately point to an object already in
    OpenCTI, so it is only reported.  With ``input_bundle`` given, a ref that
    was already dangling at the same object and field of the input stays a
    warning even in strict mode: only the refs the agent introduced fail.  The
    same holds for a stix2 spec defect of an input object (same id, same
    error), checked object by object.

    A bundle over *inline_bundle_max_chars* is answered by reference
    (``$candidate``), never echoed to the model; ``None`` always inlines it
    (a flow step, where no model context reads the answer).
    """

    # Repeat guard: an error the model cannot clear by editing the bundle (a
    # spec-pinned object, an unsupported TLP level) used to consume the whole
    # iteration budget one identical call at a time, ending in an empty draft.
    # After this many consecutive identical failures the tool answers
    # with a terminal instruction instead of another "fix and call again".
    last_signature: str | None = None
    repeat_count = 0
    # The last bundle validated, before id repair (the form the comparisons
    # read): ``patch_candidate`` applies a patch to it.
    last_candidate: dict[str, Any] | None = None

    async def _validate_stix_bundle(
        bundle_json: str = "",
        bundle: dict | None = None,
        input_bundle: dict | str | None = None,
        patch: list[object] | str | None = None,
        strict_input: bool = False,
        patch_candidate: bool = False,
    ) -> ToolResult:
        nonlocal last_signature, repeat_count, last_candidate
        errors: list[str] = []
        warnings: list[str] = []

        parsed: dict | None = None
        # With ``patch_candidate`` the patch applies to the last bundle
        # validated; ``input_bundle`` stays the input every comparison reads, so
        # a ref or a defect the agent introduced is never taken for the input's.
        patch_base: dict[str, Any] | None = None
        if patch_candidate:
            if patch is None:
                errors.append("'patch_candidate' needs a 'patch' to apply to the last bundle validated.")
            elif last_candidate is None:
                errors.append("'patch_candidate': no bundle was validated yet in this run.")
            else:
                patch_base = last_candidate
        if patch_candidate and patch_base is None:
            pass  # the refusal is in ``errors``; nothing to validate
        elif patch is not None:
            # Patch mode: the bundle is the input with the patch applied, so
            # every value outside the patch is the input's, byte for byte.
            operations: object = patch
            if isinstance(patch, str):
                try:
                    operations = json.loads(patch)
                except json.JSONDecodeError as exc:
                    operations = None
                    errors.append(f"'patch' is not valid JSON: {exc.msg}.")
            base = (
                patch_base
                if patch_base is not None
                else (_as_bundle(input_bundle) if input_bundle is not None else None)
            )
            if bundle is not None or bundle_json:
                errors.append("Give either 'patch' (applied to 'input_bundle') or 'bundle'/'bundle_json', not both.")
            elif base is None:
                errors.append("'patch' needs 'input_bundle', the bundle it applies to (pass \"$input\").")
            elif operations is not None:
                try:
                    patched = apply_json_patch(base, operations)
                except JsonPatchError as exc:
                    errors.append(f"Patch not applied: {exc}")
                else:
                    if isinstance(patched, dict):
                        parsed = patched
                    else:
                        errors.append("The patched document is not a JSON object.")
        elif bundle is not None:
            if isinstance(bundle, dict):
                parsed = bundle
            else:
                errors.append("'bundle' must be a JSON object when provided.")
        elif bundle_json:
            try:
                loaded = json.loads(bundle_json)
                if not isinstance(loaded, dict):
                    errors.append("Parsed JSON root must be an object.")
                else:
                    parsed = loaded
            except json.JSONDecodeError as exc:
                errors.append(f"Invalid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})")
        else:
            errors.append("Provide either 'bundle_json' or 'bundle'.")

        if parsed is None:
            return _answer({"valid": False, "error_count": len(errors), "errors": errors})

        original_bundle_id = parsed.get("id") if _is_valid_stix_id(parsed.get("id"), "bundle") else None

        # Pin TLP markings to their spec form (id, definition, created), then
        # repair only the ids that are actually malformed.  Renumbering valid
        # ids is what made spec-pinned objects unvalidatable.
        parsed, marking_warnings = _normalize_tlp_markings(parsed)
        candidate_before_repair = parsed
        last_candidate = candidate_before_repair
        parsed, rewrite_warnings = _rewrite_bundle_uuids(parsed)

        try:
            import stix2

            stix2.parse(parsed, allow_custom=True, version="2.1")
        except ImportError as exc:
            errors.append(f"stix2 import failed: {exc}")
        except Exception as exc:
            # With the input known, a spec defect the input already carries
            # (a report with no object_refs yet) is kept by a lossless
            # transform, so it is only a warning; one the agent introduced
            # stays an error.
            original_for_spec = _as_bundle(input_bundle) if input_bundle is not None else None
            split = (
                _split_spec_errors_against_input(stix2, parsed, original_for_spec)
                if original_for_spec is not None
                else None
            )
            if split is None:
                errors.append(f"stix2 validation error: {exc}")
            else:
                introduced_spec, inherited_spec = split
                errors.extend(_capped(introduced_spec, "stix2 validation errors"))
                warnings.extend(_capped(inherited_spec, "stix2 validation errors already present in input_bundle"))

        original_for_refs = _as_bundle(input_bundle) if strict_refs and input_bundle is not None else None
        if original_for_refs is not None:
            # Strict mode with the input known: a ref the input already left
            # dangling (an OpenCTI container pointing to an entity the platform
            # holds) is kept by a lossless transform, so it is only a warning;
            # a dangling ref the agent introduced stays an error.
            introduced, inherited = _split_dangling_against_input(parsed, original_for_refs)
            errors.extend(_capped(introduced, "unresolved refs"))
            warnings.extend(_capped(inherited, "unresolved refs already dangling in input_bundle"))
        elif strict_refs:
            errors.extend(_capped(_unresolved_refs(parsed), "unresolved refs"))
        else:
            warnings.extend(_capped(_unresolved_refs(parsed), "unresolved refs"))

        original_for_shape = _as_bundle(input_bundle) if input_bundle is not None else None
        shape_errors, shape_warnings = _objects_shape_findings(candidate_before_repair, original_for_shape)
        errors.extend(shape_errors)
        warnings.extend(shape_warnings)

        if input_bundle is not None:
            original = original_for_shape
            if original is None:
                warnings.append("'input_bundle' is not a JSON object: nothing was compared.")
            else:
                # Compared before id repair: a repaired id is reported in
                # rewrite_details, not as an object lost.
                warnings.extend(
                    _capped(_lost_against_input(candidate_before_repair, original), "losses against the input")
                )
                changed = _capped(
                    _changed_against_input(candidate_before_repair, original), "changed values against the input"
                )
                if strict_input:
                    errors.extend(changed)
                else:
                    warnings.extend(changed)

        valid = len(errors) == 0
        result: dict[str, object] = {
            "valid": valid,
            "error_count": len(errors),
            "errors": errors,
            "bundle": parsed,
        }
        if warnings:
            result["warning_count"] = len(warnings)
            result["warnings"] = warnings
        if rewrite_warnings:
            result["uuid_rewrites"] = len(rewrite_warnings)
            result["rewrite_details"] = rewrite_warnings
        if marking_warnings:
            result["marking_normalizations"] = len(marking_warnings)
            result["marking_details"] = marking_warnings

        # The signature is the error text plus the *caller-supplied* bundle id, so
        # two different bundles failing the same way keep separate counters.  A
        # bundle whose id we had to generate contributes ``None`` rather than the
        # fresh uuid4, or a bundle re-submitted without an id would look new on
        # every call and the guard would never fire.
        signature = (
            None
            if valid
            else json.dumps(
                {"bundle": original_bundle_id, "errors": sorted(errors)},
                ensure_ascii=False,
            )
        )
        if signature is not None and signature == last_signature:
            repeat_count += 1
        else:
            last_signature = signature
            repeat_count = 1

        bundle_text = json.dumps(parsed, ensure_ascii=False)
        inlined = inline_bundle_max_chars is None or len(bundle_text) <= inline_bundle_max_chars
        if valid:
            return _answer(result, inlined=inlined, bundle_chars=len(bundle_text))

        bundle_named = "the bundle above" if inlined else f"the referenced bundle ({CANDIDATE_REFERENCE})"
        if repeat_count >= _REPEAT_GUARD_THRESHOLD:
            result["terminal"] = True
            result["repeat_count"] = repeat_count
            result["next_step"] = (
                f"TERMINAL: this exact error has been returned {repeat_count} times in a row, so "
                "editing the bundle is not clearing it. Do NOT call stix_bundle_validator "
                "again for this bundle. Either drop the offending object together with every "
                f"reference to it, or return {bundle_named} as your final answer and state "
                "the unresolved error in your response."
            )
        elif inlined:
            result["next_step"] = "Fix all errors and call stix_bundle_validator again."
        else:
            result["next_step"] = (
                "Fix all errors and call stix_bundle_validator again: send a corrected patch "
                f"against your input, or patch this bundle ({CANDIDATE_REFERENCE}) with patch_candidate true."
            )
        return _answer(result, inlined=inlined, bundle_chars=len(bundle_text))

    executor = CallableToolExecutor()
    executor.register("stix_bundle_validator", _validate_stix_bundle)

    definition = ToolDefinition(
        name="stix_bundle_validator",
        description=(
            "Validate your final STIX 2.1 bundle; fix errors and validate again. TLP "
            "markings are normalised for you: never drop one. Refs to objects absent "
            "from the bundle are reported. To transform an input, pass input_bundle "
            "and a patch instead of the whole bundle. On 'terminal': true, stop and "
            "follow 'next_step'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bundle_json": {
                    "type": "string",
                    "description": "Raw STIX bundle JSON string to validate.",
                },
                "bundle": {
                    "type": "object",
                    "description": 'Parsed bundle (alternative to bundle_json), or "$workspace:PATH".',
                },
                "input_bundle": {
                    "type": "object",
                    "description": 'Bundle you transformed ("$input" when the agent config sets output_mode "patch" or input_reference true): lost and changed values are reported; its own dangling refs and spec defects stay warnings.',
                },
                "patch": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": 'RFC 6902 ops applied to input_bundle; the result is validated and returned. Large: "$workspace:PATH".',
                },
                "strict_input": {
                    "type": "boolean",
                    "description": "Changed input values are errors, not warnings.",
                },
                "patch_candidate": {
                    "type": "boolean",
                    "description": "Apply patch to the last bundle validated ($candidate), not input_bundle.",
                },
            },
        },
        source="builtin:cti",
    )

    return [definition], executor
