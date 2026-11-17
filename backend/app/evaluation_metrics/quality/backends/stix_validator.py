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

"""Standards-based :class:`StixValidator` backend, built on ``stix2``.

Validates STIX 2.1 with ``stix2.parse(..., allow_custom=True, version="2.1")``, as the
consuming XTM One platform does. A parsing failure yields a :class:`StixValidationReport`
with ``valid=False``, never an exception. Fully offline.

``stix2-validator`` is not used: its published distribution ships no JSON schemas, so every
object raises ``SchemaInvalidError``.

``stix2`` is built to create objects, so it fills in a required ``id``, ``created`` or
``modified`` the bundle omits. Those properties are checked on the bundle as
written first: each absent one is a ``missing_required_property`` finding naming the
object and the property, and a ``modified`` earlier than ``created`` is a
``modified_before_created`` finding. ``stix2.parse`` stays the judge of everything else.
"""

from collections.abc import Mapping
from datetime import datetime

try:
    import stix2
    from stix2.registry import STIX2_OBJ_MAPS
except ImportError as exc:  # pragma: no cover - exercised via sys.modules patching
    raise ImportError(
        "StandardsStixValidator requires the 'stix2' dependency, which is "
        "missing. It is declared in backend/pyproject.toml."
    ) from exc

from app.evaluation_metrics.quality.families.stix_bundle import StixValidationReport
from app.evaluation_metrics.quality.results import MetricError

# STIX 2.1 SDOs and SROs (and the language-content and extension-definition objects): each
# requires spec_version, id, created and modified. A marking definition has no modified.
_MARKING_DEFINITION = "marking-definition"
_DATED_TYPES: frozenset[str] = frozenset(STIX2_OBJ_MAPS["2.1"]["objects"]) - {"bundle", _MARKING_DEFINITION}
_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(_DATED_TYPES, ("id", "spec_version", "created", "modified")),
    _MARKING_DEFINITION: ("id", "spec_version", "created"),
}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _object_findings(obj: Mapping[str, object], location: str) -> list[MetricError]:
    """The required properties ``stix2`` would fill in that ``obj`` omits, as written."""
    kind = obj.get("type")
    required = _REQUIRED_BY_TYPE.get(kind, ("id",)) if isinstance(kind, str) else ("id",)
    name = obj.get("id") if isinstance(obj.get("id"), str) else f"a {kind} object"
    findings = [
        MetricError(
            code="missing_required_property",
            message=f"{name}: required property {prop!r} is missing",
            locations=[f"{location}.{prop}"],
        )
        for prop in required
        if prop not in obj
    ]
    created, modified = _timestamp(obj.get("created")), _timestamp(obj.get("modified"))
    if created and modified and created.tzinfo == modified.tzinfo and modified < created:
        findings.append(
            MetricError(
                code="modified_before_created",
                message=f"{name}: modified is earlier than created",
                locations=[f"{location}.modified"],
            )
        )
    return findings


def _written_findings(bundle: object) -> list[MetricError]:
    """Findings on the bundle as written, before ``stix2`` fills anything in."""
    if not isinstance(bundle, Mapping):
        return []
    if bundle.get("type") != "bundle":
        return _object_findings(bundle, "$")
    findings = _object_findings(bundle, "$")
    objects = bundle.get("objects")
    if isinstance(objects, list):
        for index, obj in enumerate(objects):
            if isinstance(obj, Mapping):
                findings += _object_findings(obj, f"objects[{index}]")
    return findings


class StandardsStixValidator:
    """Validate a STIX 2.1 bundle with the official ``stix2`` parser."""

    def validate(self, bundle: object) -> StixValidationReport:
        """Return a structured STIX 2.1 validation report.

        Args:
            bundle: The candidate bundle (already-parsed JSON).

        Returns:
            A report whose ``valid`` flag says whether the bundle as written holds every
            required property and ``stix2`` accepted it. A parsing failure is a
            ``stix_parse_error`` finding, not an exception.
        """
        findings = _written_findings(bundle)
        try:
            stix2.parse(bundle, allow_custom=True, version="2.1")
        except Exception as exc:
            findings.append(
                MetricError(
                    code="stix_parse_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
        return StixValidationReport(valid=not findings, findings=findings)


__all__ = ["StandardsStixValidator"]
