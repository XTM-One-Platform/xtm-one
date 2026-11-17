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

"""How the scorer reads an expected answer or an answer, for the pages that compare them.

Two needs, both answered with the scorer's own reading so a page never disagrees with a
score:

* :func:`reference_key` pairs entries of two datasets: two expected answers share a key
  when the family's scorer reads them as the same ground truth (for STIX, the same
  canonical objects and relationship triples, whatever the identifiers, timestamps or
  object order of the two files).
* :func:`canonical_stix_bundle` is what the STIX scorer compares: the bundle's scored
  objects with their canonical identifiers, and its relationships and sightings as
  canonical ``(source, type, target)`` triples. The bundle review is built on it.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.services.evaluation.metric_package.availability import guard_package_call
from app.services.evaluation.metric_package.families import STIX_BUNDLE
from app.services.evaluation.metric_package.quality_adapter import (
    QualityConfigError,
    _parse_stix_prediction,
    _parse_stix_reference,
)


@dataclass(frozen=True)
class CanonicalBundle:
    """A STIX bundle as the scorer compares it."""

    #: The scored objects (the entity selection's types), with canonical ``id`` and refs.
    objects: tuple[Mapping[str, Any], ...]
    #: Relationships as ``(source, type, target)``; a sighting gives one triple per sighted
    #: location, typed ``sighting``.
    triples: tuple[tuple[str, str, str], ...]


@lru_cache(maxsize=1)
def _stix_backend() -> tuple[Any, Any] | None:
    """The scorer's canonicalizer and default object selection, or ``None`` when the STIX
    backend cannot load (an image without ``libmagic``): the pages then say so."""

    def build() -> tuple[Any, Any] | None:
        from app.evaluation_metrics.quality.families.stix_bundle.canonical import (
            StixObjectTypeSelection,
        )

        try:
            from app.evaluation_metrics.quality.backends.stix_opencti import (
                OpenCtiCanonicalizer,
            )

            return OpenCtiCanonicalizer(), StixObjectTypeSelection()
        except Exception:
            return None

    return guard_package_call("STIX canonical view", build)


def stix_backend_available() -> bool:
    """Whether the STIX canonicalizer can be built in this process."""
    return _stix_backend() is not None


def canonical_stix_bundle(raw: object, *, reference: bool = False) -> CanonicalBundle | None:
    """The bundle *raw* as the STIX scorer compares it, or ``None`` when the scorer cannot
    read it (not JSON, not a bundle) or the STIX backend is unavailable.

    *reference* parses *raw* as ground truth (a dataset's expected answer), else as an
    agent's answer: both as the scorer does, so an answer the scorer scores 0 as malformed
    has no canonical view here either.
    """
    backend = _stix_backend()
    if backend is None:
        return None
    canonicalizer, selection = backend
    try:
        bundle = _parse_stix_reference(raw) if reference else _parse_stix_prediction(raw)
    except QualityConfigError:
        return None
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("objects"), list):
        return None
    try:
        canonical = canonicalizer.canonicalize(bundle)
    except Exception:
        return None
    if not isinstance(canonical, Mapping) or not isinstance(canonical.get("objects"), list):
        return None
    objects: list[Mapping[str, Any]] = []
    triples: list[tuple[str, str, str]] = []
    for obj in canonical["objects"]:
        if not isinstance(obj, Mapping):
            continue
        kind = obj.get("type")
        if kind == "relationship":
            ends = (obj.get("source_ref"), obj.get("relationship_type"), obj.get("target_ref"))
            if all(isinstance(part, str) for part in ends):
                triples.append((ends[0], ends[1], ends[2]))
        elif kind == "sighting":
            sighted = obj.get("sighting_of_ref")
            where = obj.get("where_sighted_refs") or [""]
            if isinstance(sighted, str) and isinstance(where, list):
                triples.extend((sighted, "sighting", str(w)) for w in where)
        elif isinstance(kind, str) and isinstance(obj.get("id"), str) and selection.includes(kind):
            objects.append(obj)
    return CanonicalBundle(objects=tuple(objects), triples=tuple(triples))


def reference_key(family: str | None, reference: object) -> str | None:
    """A key two expected answers share when *family*'s scorer reads them as the same
    ground truth, or ``None`` when it cannot read this one.

    STIX: the canonical object identifiers and relationship triples. Any other family: the
    JSON value with sorted keys when the answer is JSON, else its text (NFC, trimmed).
    """
    if family == STIX_BUNDLE:
        view = canonical_stix_bundle(reference, reference=True)
        if view is None:
            return None
        material: object = {
            "objects": sorted(str(obj["id"]) for obj in view.objects),
            "triples": sorted(list(t) for t in view.triples),
        }
    else:
        text = reference if isinstance(reference, str) else json.dumps(reference, sort_keys=True)
        try:
            material = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            material = unicodedata.normalize("NFC", text).strip()
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalBundle",
    "canonical_stix_bundle",
    "reference_key",
    "stix_backend_available",
]
