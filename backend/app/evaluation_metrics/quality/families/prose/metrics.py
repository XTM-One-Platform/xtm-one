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

"""PROSE family metrics: deterministic, lexical scoring of free-text output.

Five metrics: normalized exact match, token-level precision/recall/F1, ROUGE-L over the
longest common subsequence, required-phrase coverage and forbidden-phrase absence. All
comparison is lexical: no semantic accuracy, fact or claim verification, entailment or
hallucination detection, and results must never be presented as such.

Every metric resolves the same fixed precedence on its own, with no cross-metric gating:

1. configuration validity -> ``ConfigurationError`` before any result is built;
2. configuration presence -> ``skipped``, decided *before* the prediction is examined;
3. prediction usability -> ``invalid``;
   3a. normalization and tokenization of both sides;
   3b. zero tokens on both sides -> ``skipped``;
   3c. ``prose.rouge_l`` only: token product over the bound -> ``skipped``;
4. otherwise -> ``scored``.

Step 2 precedes step 3: ``invalid`` on a metric the caller never configured would blame
the model for a measurement nobody asked for. Step 3b precedes 3c so the bound cannot
capture empty-input semantics.

Error text is built from a closed value set. ``MetricError`` is serialized into the
result, so ``repr()`` of a caller object would embed a memory address and break every
golden fixture.
"""

from collections.abc import Sequence

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.prose.phrases import (
    PreparedPhrases,
    contains_phrase,
    prepare_phrases,
)
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy
from app.evaluation_metrics.quality.primitives.pass_fail import pass_fail_counts
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import multiset_diagnostics
from app.evaluation_metrics.quality.results import (
    JSONValue,
    MetricDirection,
    MetricError,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_EXACT_META = MetricMetadata(
    key="prose.exact_match",
    label="Exact Match (Normalised)",
    kind="match",
    primary_measure="exact_match",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)

_TOKEN_PRF_META = MetricMetadata(
    key="prose.token_prf",
    label="Word Overlap F1",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.mean,
)

_ROUGE_L_META = MetricMetadata(
    key="prose.rouge_l",
    label="Word Sequence Overlap (ROUGE-L)",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.mean,
)

_REQUIRED_META = MetricMetadata(
    key="prose.required_phrase_coverage",
    label="Required Phrases Present",
    kind="coverage",
    primary_measure="coverage",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.mean,
)

_FORBIDDEN_META = MetricMetadata(
    key="prose.forbidden_phrase_absence",
    label="Forbidden Phrases Absent",
    kind="coverage",
    primary_measure="absence",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.mean,
)

_INPUT_INVALID = "prose_input_invalid"

_MAX_LCS_TOKEN_PRODUCT = 5_000_000
"""Largest ``reference_tokens * prediction_tokens`` ``prose.rouge_l`` will compute.

Private, but *observable*: it decides ``scored`` versus ``skipped`` and is serialized as
``max_token_product`` into results and golden fixtures. Changing it is a versioned contract
change: update the boundary tests, regenerate the fixtures, and give the old and new values
in a release note.
"""


def _skipped(meta: MetricMetadata, reason: str) -> ScoreResult:
    return ScoreResult(
        status=MetricStatus.skipped,
        value=None,
        detail={"reason": reason},
        metadata=meta,
    )


def _invalid_prediction(meta: MetricMetadata, prediction: object) -> ScoreResult:
    received = type(prediction).__name__
    return ScoreResult(
        status=MetricStatus.invalid,
        value=0,
        error=MetricError(
            code=_INPUT_INVALID,
            message=f"prose prediction must be str, got {received}",
            locations=[],
            details={"received_type": received},
        ),
        metadata=meta,
    )


def _resolve_reference(reference: object, meta: MetricMetadata) -> str | ScoreResult:
    """Resolve the reference to text, a ``skipped`` result, or a raised error.

    An absent reference is a valid phrase-only evaluation and yields ``skipped``. A present
    non-text reference is ground truth, not model output, so it raises.
    """
    if reference is None:
        return _skipped(meta, "reference_not_configured")
    if not isinstance(reference, str):
        raise ConfigurationError(
            f"prose reference must be str or None, got {type(reference).__name__}"
        )
    if not reference.strip():
        # An empty expected answer is a draft, a configuration problem: never a score
        # for the agent, whatever it answers.
        return _skipped(meta, "reference_empty")
    return reference


def prose_exact_match(
    reference: object, prediction: object, policy: ProseNormalizationPolicy
) -> ScoreResult:
    """Score whether ``prediction`` equals ``reference`` after normalization.

    Args:
        reference: The reference prose, or ``None`` when not configured.
        prediction: The predicted prose.
        policy: The normalization policy applied to both sides.

    Returns:
        ``scored`` 100 on an exact post-normalization match and 0 otherwise; ``skipped``
        with reason ``reference_not_configured`` when no reference is supplied;
        ``invalid`` with code ``prose_input_invalid`` when the prediction is not text.

    Raises:
        ConfigurationError: If ``reference`` is present but not ``str``.
    """
    resolved = _resolve_reference(reference, _EXACT_META)
    if isinstance(resolved, ScoreResult):
        return resolved
    if not isinstance(prediction, str):
        return _invalid_prediction(_EXACT_META, prediction)

    normalized_reference = policy.normalize(resolved)
    normalized_prediction = policy.normalize(prediction)
    match = normalized_reference == normalized_prediction
    detail: dict[str, JSONValue] = {
        "match": match,
        "reference_length": len(normalized_reference),
        "prediction_length": len(normalized_prediction),
        "normalization": dict(policy.as_detail()),
        **pass_fail_counts(match),
    }
    return ScoreResult(
        status=MetricStatus.scored,
        value=100.0 if match else 0.0,
        detail=detail,
        metadata=_EXACT_META,
    )


def prose_token_prf(
    reference: object, prediction: object, policy: ProseNormalizationPolicy
) -> ScoreResult:
    """Score token-level precision, recall and F1 as a multiset overlap.

    Args:
        reference: The reference prose, or ``None`` when not configured.
        prediction: The predicted prose.
        policy: The normalization policy applied to both sides.

    Returns:
        A single ``scored`` result whose value is F1 on the 0-100 scale, with precision,
        recall, F1, ``tp``/``fp``/``fn`` and both token totals in ``detail``. Returns
        ``skipped`` with reason ``reference_not_configured`` when no reference is
        supplied, ``skipped`` with reason ``no_tokens_on_either_side`` when both sides
        normalize to zero tokens, and ``invalid`` when the prediction is not text.

    Raises:
        ConfigurationError: If ``reference`` is present but not ``str``.
    """
    resolved = _resolve_reference(reference, _TOKEN_PRF_META)
    if isinstance(resolved, ScoreResult):
        return resolved
    if not isinstance(prediction, str):
        return _invalid_prediction(_TOKEN_PRF_META, prediction)

    reference_tokens = policy.tokenize(resolved)
    prediction_tokens = policy.tokenize(prediction)
    if not reference_tokens and not prediction_tokens:
        return _skipped(_TOKEN_PRF_META, "no_tokens_on_either_side")

    diagnostics = multiset_diagnostics(reference_tokens, prediction_tokens)
    precision, recall, f1 = precision_recall_f1(
        diagnostics.tp, diagnostics.fp, diagnostics.fn
    )
    detail: dict[str, JSONValue] = {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        "tp": diagnostics.tp,
        "fp": diagnostics.fp,
        "fn": diagnostics.fn,
        "reference_tokens": len(reference_tokens),
        "prediction_tokens": len(prediction_tokens),
        "normalization": dict(policy.as_detail()),
    }
    return ScoreResult(
        status=MetricStatus.scored,
        value=f1 * 100.0,
        detail=detail,
        metadata=_TOKEN_PRF_META,
    )


def _lcs_length(reference_tokens: Sequence[str], prediction_tokens: Sequence[str]) -> int:
    """Return the length of the longest common subsequence of two token sequences.

    Computing only the length lets the DP table shrink to two rows sized by the shorter
    sequence: ``O(min(r, p))`` space, no quadratic allocation.
    """
    outer, inner = reference_tokens, prediction_tokens
    if len(inner) > len(outer):
        outer, inner = inner, outer
    if not inner:
        return 0

    previous = [0] * (len(inner) + 1)
    current = [0] * (len(inner) + 1)
    for outer_token in outer:
        for j, inner_token in enumerate(inner, start=1):
            if outer_token == inner_token:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous, current = current, previous
    return previous[len(inner)]


def _rouge_l_too_large(reference_tokens: int, prediction_tokens: int) -> ScoreResult:
    """Build the bounded skip, which is the only skip carrying more than a reason."""
    return ScoreResult(
        status=MetricStatus.skipped,
        value=None,
        detail={
            "reason": "rouge_l_input_too_large",
            "reference_tokens": reference_tokens,
            "prediction_tokens": prediction_tokens,
            "token_product": reference_tokens * prediction_tokens,
            "max_token_product": _MAX_LCS_TOKEN_PRODUCT,
        },
        metadata=_ROUGE_L_META,
    )


def prose_rouge_l(
    reference: object, prediction: object, policy: ProseNormalizationPolicy
) -> ScoreResult:
    """Score lexical, order-sensitive token overlap as ROUGE-L.

    The longest common subsequence ``L`` of the two normalized token sequences, reported
    as precision ``L/p``, recall ``L/r`` and their harmonic mean. It is not factual
    correctness, faithfulness, semantic similarity or answer quality, and must never be
    described as such.

    Args:
        reference: The reference prose, or ``None`` when not configured.
        prediction: The predicted prose.
        policy: The normalization policy applied to both sides.

    Returns:
        A single ``scored`` result whose value is F1 on the 0-100 scale, with precision,
        recall, F1, the LCS length and both token totals in ``detail``. Returns ``skipped``
        with reason ``reference_not_configured`` when no reference is supplied,
        ``no_tokens_on_either_side`` when both sides normalize to zero tokens,
        ``rouge_l_input_too_large`` when the token product exceeds the computation bound,
        and ``invalid`` when the prediction is not text.

    Raises:
        ConfigurationError: If ``reference`` is present but not ``str``.
    """
    resolved = _resolve_reference(reference, _ROUGE_L_META)
    if isinstance(resolved, ScoreResult):
        return resolved
    if not isinstance(prediction, str):
        return _invalid_prediction(_ROUGE_L_META, prediction)

    reference_tokens = policy.tokenize(resolved)
    prediction_tokens = policy.tokenize(prediction)
    if not reference_tokens and not prediction_tokens:
        return _skipped(_ROUGE_L_META, "no_tokens_on_either_side")
    # Checked before any LCS work: skipping after computing would defeat the bound.
    if len(reference_tokens) * len(prediction_tokens) > _MAX_LCS_TOKEN_PRODUCT:
        return _rouge_l_too_large(len(reference_tokens), len(prediction_tokens))

    lcs_length = _lcs_length(reference_tokens, prediction_tokens)
    precision, recall, f1 = precision_recall_f1(
        lcs_length,
        len(prediction_tokens) - lcs_length,
        len(reference_tokens) - lcs_length,
    )
    detail: dict[str, JSONValue] = {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        "lcs_length": lcs_length,
        "reference_tokens": len(reference_tokens),
        "prediction_tokens": len(prediction_tokens),
        "normalization": dict(policy.as_detail()),
    }
    return ScoreResult(
        status=MetricStatus.scored,
        value=f1 * 100.0,
        detail=detail,
        metadata=_ROUGE_L_META,
    )


def _phrase_breakdown(
    prepared: PreparedPhrases, prediction_tokens: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    unmatched: list[str] = []
    for phrase, tokens in zip(prepared.phrases, prepared.tokens, strict=True):
        if contains_phrase(prediction_tokens, tokens):
            matched.append(phrase)
        else:
            unmatched.append(phrase)
    return matched, unmatched


def _phrase_detail(
    matched: list[str],
    unmatched: list[str],
    prepared: PreparedPhrases,
    policy: ProseNormalizationPolicy,
) -> dict[str, JSONValue]:
    return {
        "matched": list(matched),
        "unmatched": list(unmatched),
        "total": len(prepared.phrases),
        "duplicates_removed": prepared.duplicates_removed,
        "normalization": dict(policy.as_detail()),
    }


def _score_phrases(
    meta: MetricMetadata,
    prediction: object,
    phrases: Sequence[str] | None,
    policy: ProseNormalizationPolicy,
    list_name: str,
    reward_matched: bool,
) -> ScoreResult:
    if phrases is None:
        return _skipped(meta, f"{list_name}_not_configured")
    prepared = prepare_phrases(phrases, policy, list_name)
    if not prepared.phrases:
        return _skipped(meta, f"{list_name}_empty")
    if not isinstance(prediction, str):
        return _invalid_prediction(meta, prediction)

    prediction_tokens = policy.tokenize(prediction)
    matched, unmatched = _phrase_breakdown(prepared, prediction_tokens)
    rewarded = matched if reward_matched else unmatched
    value = len(rewarded) / len(prepared.phrases) * 100.0
    return ScoreResult(
        status=MetricStatus.scored,
        value=value,
        detail=_phrase_detail(matched, unmatched, prepared, policy),
        metadata=meta,
    )


def prose_required_phrase_coverage(
    prediction: object,
    phrases: Sequence[str] | None,
    policy: ProseNormalizationPolicy,
) -> ScoreResult:
    """Score the fraction of caller-supplied required phrases present in the prediction.

    Args:
        prediction: The predicted prose.
        phrases: The required phrases, ``()`` when explicitly empty, or ``None`` when not
            configured. The two are distinct configurations with distinct reason codes.
        policy: The normalization policy applied to the prediction and every phrase.

    Returns:
        ``scored`` with the matched fraction on the 0-100 scale and the matched/unmatched
        breakdown in ``detail``; ``skipped`` when the list is absent or empty; ``invalid``
        when the prediction is not text. Matching is token-sequence containment.

    Raises:
        ConfigurationError: If an element is not ``str``, or a phrase is empty or
            normalizes to zero tokens under ``policy``.
    """
    return _score_phrases(
        _REQUIRED_META, prediction, phrases, policy, "required_phrases", True
    )


def prose_forbidden_phrase_absence(
    prediction: object,
    phrases: Sequence[str] | None,
    policy: ProseNormalizationPolicy,
) -> ScoreResult:
    """Score the fraction of caller-supplied forbidden phrases absent from the prediction.

    Args:
        prediction: The predicted prose.
        phrases: The forbidden phrases, ``()`` when explicitly empty, or ``None`` when not
            configured. The two are distinct configurations with distinct reason codes.
        policy: The normalization policy applied to the prediction and every phrase.

    Returns:
        ``scored`` with the absent fraction on the 0-100 scale (higher is better);
        ``detail`` has the coverage shape, ``matched`` listing the violations. ``skipped``
        when the list is absent or empty; ``invalid`` when the prediction is not text.
        Matching is token-sequence containment.

    Raises:
        ConfigurationError: If an element is not ``str``, or a phrase is empty or
            normalizes to zero tokens under ``policy``.
    """
    return _score_phrases(
        _FORBIDDEN_META, prediction, phrases, policy, "forbidden_phrases", False
    )


__all__ = [
    "prose_exact_match",
    "prose_forbidden_phrase_absence",
    "prose_required_phrase_coverage",
    "prose_rouge_l",
    "prose_token_prf",
]
