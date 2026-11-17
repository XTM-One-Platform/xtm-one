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

"""Declarative, prediction-side output extraction for evaluations."""

from .canonical import canonicalise, normalise_label_set
from .engine import ExtractionOutcome, apply_extractor
from .failures import (
    EXTRACTION_FAILED_KEY,
    FAILURE_REASONS,
    ExtractionFailure,
    count_extraction_failures,
    failure_metric_blob,
)
from .limits import ExtractionLimits, current_limits
from .record import (
    EXTRACTED_PREDICTION_KEY,
    ExtractedPrediction,
    item_count,
    record_extracted_prediction,
)
from .sources import last_successful_call, resolve_source
from .spec import (
    OPERATIONS,
    OUTPUT_KINDS,
    SOURCE_KINDS,
    supported_output_kinds,
    validate_extractor_chain,
    validate_output_kind_for_family,
)

__all__ = [
    "EXTRACTED_PREDICTION_KEY",
    "EXTRACTION_FAILED_KEY",
    "FAILURE_REASONS",
    "OPERATIONS",
    "OUTPUT_KINDS",
    "SOURCE_KINDS",
    "ExtractedPrediction",
    "ExtractionFailure",
    "ExtractionLimits",
    "ExtractionOutcome",
    "apply_extractor",
    "canonicalise",
    "count_extraction_failures",
    "current_limits",
    "failure_metric_blob",
    "item_count",
    "last_successful_call",
    "normalise_label_set",
    "record_extracted_prediction",
    "resolve_source",
    "supported_output_kinds",
    "validate_extractor_chain",
    "validate_output_kind_for_family",
]
