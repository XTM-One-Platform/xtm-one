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

"""Phrase preparation and token-sequence containment for the PROSE family.

Preparation normalizes, validates and deduplicates in one place so the direct and routed
paths cannot diverge. Validation runs *after* normalization, so validity depends on the
policy: ``"!!!"`` is a valid phrase under ``punctuation="preserve"`` and raises under
``strip`` or ``edges``, which leave it with zero tokens that can never match.

Matching is contiguous token-sequence containment, never substring search, so ``cat``
does not match inside ``concatenate``.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy


@dataclass(frozen=True, slots=True)
class PreparedPhrases:
    """Normalized, validated, deduplicated phrases with their token tuples."""

    phrases: tuple[str, ...]
    tokens: tuple[tuple[str, ...], ...]
    duplicates_removed: int


def prepare_phrases(
    raw: Sequence[str], policy: ProseNormalizationPolicy, list_name: str
) -> PreparedPhrases:
    """Normalize, validate and deduplicate ``raw`` under ``policy``.

    Args:
        raw: The caller-supplied phrases, in caller order.
        policy: The normalization policy applied to every phrase.
        list_name: The configuration field name, used in error messages.

    Returns:
        The deduplicated normalized phrases in first-occurrence order, their token
        tuples positionally aligned, and the number of duplicates removed.

    Raises:
        ConfigurationError: If ``raw`` is a bare ``str`` rather than a sequence of
            phrases, if an element is not ``str``, or if a phrase is empty or normalizes
            to zero tokens under ``policy``.
    """
    # A ``str`` is itself a ``Sequence[str]``: iterating it would make each character
    # a phrase.
    if isinstance(raw, str):
        raise ConfigurationError(
            f"{list_name} must be a sequence of phrases, not a single str; "
            f"pass ({raw!r},) to supply one phrase"
        )
    seen: dict[str, tuple[str, ...]] = {}
    duplicates_removed = 0
    for index, element in enumerate(raw):
        if not isinstance(element, str):
            raise ConfigurationError(
                f"{list_name}[{index}] must be str, got {type(element).__name__}"
            )
        tokens = policy.tokenize(element)
        if not tokens:
            raise ConfigurationError(
                f"{list_name}[{index}] normalizes to zero tokens under the selected "
                "policy and can never match"
            )
        normalized = policy.normalize(element)
        if normalized in seen:
            duplicates_removed += 1
            continue
        seen[normalized] = tokens
    return PreparedPhrases(
        phrases=tuple(seen),
        tokens=tuple(seen.values()),
        duplicates_removed=duplicates_removed,
    )


def contains_phrase(
    prediction_tokens: Sequence[str], phrase_tokens: Sequence[str]
) -> bool:
    """Return whether ``phrase_tokens`` occurs as a contiguous run in ``prediction_tokens``.

    Args:
        prediction_tokens: The prediction's normalized token sequence.
        phrase_tokens: The phrase's normalized token sequence; never empty.

    Returns:
        ``True`` when the phrase tokens appear consecutively, compared by exact token
        equality at every position. Matching consumes nothing, so overlapping phrases
        can all match.
    """
    span = len(phrase_tokens)
    if span == 0 or span > len(prediction_tokens):
        return False
    first = phrase_tokens[0]
    for start in range(len(prediction_tokens) - span + 1):
        if prediction_tokens[start] != first:
            continue
        # Element-wise: no allocation per window, and correct when the two arguments are
        # different concrete Sequence types.
        if all(
            prediction_tokens[start + offset] == phrase_tokens[offset]
            for offset in range(1, span)
        ):
            return True
    return False


__all__ = ["PreparedPhrases", "contains_phrase", "prepare_phrases"]
