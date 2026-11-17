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

"""Deterministic quality metrics. Shared contracts live in :mod:`evaluation_metrics.core`."""

from app.evaluation_metrics.quality.families.classification import (
    classification_dataset,
    label_vocabulary_validity,
)
from app.evaluation_metrics.quality.families.prose import (
    ProseConfig,
    prose_exact_match,
    prose_forbidden_phrase_absence,
    prose_required_phrase_coverage,
    prose_rouge_l,
    prose_token_prf,
)
from app.evaluation_metrics.quality.families.routing import Family, FamilyConfig, run_for_family
from app.evaluation_metrics.quality.families.stix_bundle import (
    Canonicalizer,
    StixObjectTypeSelection,
    StixValidationReport,
    StixValidator,
    stix_entity_scores,
    stix_envelope_validity,
    stix_relationship_scores,
    stix_semantic_validity,
)
from app.evaluation_metrics.quality.families.structured_json import (
    json_field_accuracy,
    json_schema_validity,
)
from app.evaluation_metrics.quality.normalization.policies import ArraySemantics, ComparisonPolicy
from app.evaluation_metrics.quality.normalization.prose import (
    CaseMode,
    ProseNormalizationPolicy,
    PunctuationMode,
    UnicodeForm,
    WhitespaceMode,
    trim_edge_marks,
)
from app.evaluation_metrics.quality.primitives.aggregate import macro_average
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import (
    DiagnosticSet,
    multiset_diagnostics,
    set_diagnostics,
)
from app.evaluation_metrics.quality.results import MetricMetadata, ScoreResult
from app.evaluation_metrics.quality.unreadable import unreadable_answer

__all__ = [
    "ArraySemantics",
    "Canonicalizer",
    "CaseMode",
    "ComparisonPolicy",
    "DiagnosticSet",
    "Family",
    "FamilyConfig",
    "MetricMetadata",
    "ProseConfig",
    "ProseNormalizationPolicy",
    "PunctuationMode",
    "ScoreResult",
    "StixObjectTypeSelection",
    "StixValidationReport",
    "StixValidator",
    "UnicodeForm",
    "WhitespaceMode",
    "classification_dataset",
    "json_field_accuracy",
    "json_schema_validity",
    "label_vocabulary_validity",
    "macro_average",
    "multiset_diagnostics",
    "precision_recall_f1",
    "prose_exact_match",
    "prose_forbidden_phrase_absence",
    "prose_required_phrase_coverage",
    "prose_rouge_l",
    "prose_token_prf",
    "run_for_family",
    "set_diagnostics",
    "stix_entity_scores",
    "stix_envelope_validity",
    "stix_relationship_scores",
    "stix_semantic_validity",
    "trim_edge_marks",
    "unreadable_answer",
]
