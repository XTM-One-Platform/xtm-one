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

"""The platform's single source of truth for the evaluation family catalog.

The four family ids are declared here verbatim from the package ``Family`` enum, plus
platform-authored per-family display metadata (label, description, structured-output
flag, advertised metric keys). The engine and API consume the platform-neutral
projections below (``family_is_valid``, ``list_families``), never the package enum,
which stays adapter-internal. Every declared family computes deterministic metrics.

``tests/test_evaluation_families.py`` keeps this catalog in lockstep with the package:
the declared id set must equal ``{m.value for m in Family}``, so an upstream
add/remove/rename fails loudly.
"""

from __future__ import annotations

from typing import Final

# Package types are allowed HERE (adapter-internal) but must not be re-exported.
from app.evaluation_metrics.quality import Family

# --- Family id constants (verbatim from the package Family enum) -----------------------

STIX_BUNDLE: Final = "STIX_BUNDLE"
STRUCTURED_JSON: Final = "STRUCTURED_JSON"
CLASSIFICATION: Final = "CLASSIFICATION"
PROSE: Final = "PROSE"

# Families whose package config reads a caller-supplied ``json_schema``, the only ones for
# which the UI exposes the output-schema input. STIX is excluded: its validity is judged
# against the fixed STIX 2.1 standard, not an author schema.
_STRUCTURED_OUTPUT_FAMILIES: Final[frozenset[str]] = frozenset({STRUCTURED_JSON})


class _FamilyInfo:
    """Platform-authored display metadata for one family."""

    __slots__ = ("description", "id", "label", "metric_keys")

    def __init__(
        self, id: str, label: str, description: str, metric_keys: tuple[str, ...]
    ) -> None:
        self.id = id
        self.label = label
        self.description = description
        self.metric_keys = metric_keys


# Advertised metric keys are the stable keys a run of each family stores, shown in the UI
# catalog only; the results always come from the package at run time.
# ``tests/test_evaluation_metric_catalog.py`` pins each tuple to what ``run_quality``
# returns for the family, so a key no run produces cannot be advertised.
_CATALOG: Final[dict[str, _FamilyInfo]] = {
    STIX_BUNDLE: _FamilyInfo(
        STIX_BUNDLE,
        "STIX Bundle",
        "STIX 2.1 bundles, compared with a reference bundle object by object and "
        "relationship by relationship, sightings included.",
        (
            "stix.envelope_validity",
            "stix.semantic_validity",
            "stix.entity_prf",
            "stix.entity_prf.by_type",
            "stix.relationship_prf",
            "stix.entity_prf.aligned",
            "stix.entity_prf.soft",
            "stix.relationship_prf.aligned",
            "stix.entity_prf.aligned.closed",
            "stix.relationship_prf.aligned.closed",
        ),
    ),
    STRUCTURED_JSON: _FamilyInfo(
        STRUCTURED_JSON,
        # The only JSON family: detection rules and free-form JSON analysis
        # land here. The id stays "STRUCTURED_JSON" so stored rows keep resolving.
        "JSON",
        "Any JSON answer (analysis, detection rules, structured extractions) compared "
        "with a reference object field by field, each field as its schema declares it.",
        (
            "json.schema_validity",
            "json.field_accuracy",
        ),
    ),
    CLASSIFICATION: _FamilyInfo(
        CLASSIFICATION,
        "Classification",
        "Multi-label classification, compared with a reference label set.",
        (
            "classification.micro_prf",
            "classification.macro_prf",
            "classification.samples_f1",
            "classification.subset_accuracy",
            "classification.vocabulary_validity",
        ),
    ),
    PROSE: _FamilyInfo(
        PROSE,
        "Prose",
        "Natural-language prose, compared with a reference answer. Comparison ignores case "
        "and the punctuation around a word (a trailing period, quotes, brackets, bold), "
        "keeps the punctuation inside it (evil.com, T1059.001), and splits words on "
        "whitespace: scripts that do not use spaces (Chinese, Japanese, Thai) compare as "
        "a single token, so the graded metrics behave like exact match there.",
        (
            "prose.exact_match",
            "prose.token_prf",
            "prose.rouge_l",
        ),
    ),
}

# The declared id set, in package declaration order. The guard test pins this to the package.
FAMILY_IDS: Final[tuple[str, ...]] = tuple(_CATALOG)


def resolve_family(stored: str | None) -> Family:
    """Map a stored ``Evaluation.family`` string 1:1 onto the package ``Family`` enum.

    INTERNAL: the returned enum is never exported from ``__init__``. Raises a platform
    ``ValueError`` (surfaced as a 4xx config error) on an unknown or ``None`` value, never
    a silent default.
    """
    if stored is None:
        raise ValueError("family is required and was not set")
    try:
        return Family(stored)
    except ValueError as exc:
        raise ValueError(
            f"unknown evaluation family {stored!r}; expected one of {list(FAMILY_IDS)}"
        ) from exc


def family_is_valid(stored: str) -> bool:
    """True iff ``stored`` is one of the recognized family ids."""
    return stored in _CATALOG


def list_families() -> list[dict]:
    """Platform-neutral family catalog for the read-only API.

    Each entry: ``{id, label, description, metric_keys, structured_output}``.
    """
    return [
        {
            "id": info.id,
            "label": info.label,
            "description": info.description,
            "metric_keys": list(info.metric_keys),
            "structured_output": info.id in _STRUCTURED_OUTPUT_FAMILIES,
        }
        for info in _CATALOG.values()
    ]


__all__ = [
    "FAMILY_IDS",
    "family_is_valid",
    "list_families",
    "resolve_family",
]
