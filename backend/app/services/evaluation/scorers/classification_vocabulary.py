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

"""Write-time check of a dataset entry's reference labels against a declared vocabulary.

Same shape as ``structured_output.validate_output_schema``: a validator returning
an optional error message, called from the same two dataset entry write endpoints.

Labels are parsed and compared by the scoring seam's own rule
(``quality_adapter.parse_labels``), so an author's input is accepted exactly when it
will score: ``Phishing`` passes against a vocabulary declaring ``phishing``.
"""

from __future__ import annotations

from app.services.evaluation.metric_package.quality_adapter import (
    normalize_label,
    parse_labels,
)


def validate_reference_labels(
    vocabulary: frozenset[str] | list[str] | None,
    expected_answer: str | None,
) -> str | None:
    """Check that ``expected_answer``'s labels are a subset of ``vocabulary``.

    Returns ``None`` (accept) when ``vocabulary`` is ``None``/empty, or when every label
    parsed from ``expected_answer`` is in it, both normalized. Otherwise returns an error
    message naming the offending label(s), which the caller turns into a 422. A ``None``
    ``expected_answer`` parses as no labels.
    """
    if not vocabulary:
        return None
    allowed = frozenset(normalize_label(label) for label in vocabulary)
    offending = sorted(parse_labels(expected_answer) - allowed)
    if not offending:
        return None
    # Quoted: a label may hold a comma, and the list would read as more labels.
    return "Label(s) not in dataset vocabulary: " + ", ".join(f'"{label}"' for label in offending)
