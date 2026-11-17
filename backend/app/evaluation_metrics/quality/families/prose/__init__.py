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

"""PROSE family: deterministic, lexical prose-output metrics.

Scoring is lexical only. Nothing here performs semantic accuracy scoring, fact or claim
verification, entailment, or hallucination detection.
"""

from app.evaluation_metrics.quality.families.prose.config import ProseConfig
from app.evaluation_metrics.quality.families.prose.metrics import (
    prose_exact_match,
    prose_forbidden_phrase_absence,
    prose_required_phrase_coverage,
    prose_rouge_l,
    prose_token_prf,
)
from app.evaluation_metrics.quality.normalization.prose import (
    CaseMode,
    ProseNormalizationPolicy,
    PunctuationMode,
    UnicodeForm,
    WhitespaceMode,
)

__all__ = [
    "CaseMode",
    "ProseConfig",
    "ProseNormalizationPolicy",
    "PunctuationMode",
    "UnicodeForm",
    "WhitespaceMode",
    "prose_exact_match",
    "prose_forbidden_phrase_absence",
    "prose_required_phrase_coverage",
    "prose_rouge_l",
    "prose_token_prf",
]
