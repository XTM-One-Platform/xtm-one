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

"""Nothing expected and nothing given is a correct answer.

An entry whose reference and prediction are both empty (no labels, no STIX objects, no
relationships, ``{}`` against ``{}``) is answered exactly right. P/R/F1 would say ``0.0``,
because all three denominators are zero. One rule for every family:

- an averaged metric scores the entry ``EMPTY_ANSWER_SCORE``;
- a pooled metric's entry adds 0 of 0, so it moves nothing and a run whose every entry is
  empty reads full.

PROSE is the exception: an empty expected answer is a draft, a configuration problem, never
a score (``prose.metrics``).

The rule lives here, not in :func:`precision_recall_f1`. ``aggregate.macro_average`` calls
that primitive per label, where ``tp == fp == fn == 0`` means that label appears on neither
side; returning 1.0 there would score every unused label as perfect and inflate macro. Only
the family knows the whole entry is empty on both sides.
"""

from app.evaluation_metrics.core.contracts import POOLED_DENOMINATOR, POOLED_NUMERATOR
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1

EMPTY_ANSWER_SCORE = 100.0


def is_empty_answer(tp: int, fp: int, fn: int) -> bool:
    """True when nothing was expected and nothing was predicted.

    ``tp + fp + fn`` is the size of the union of the two sets, so a zero sum can only mean
    both were empty, not merely that the answer was wrong.
    """
    return tp == 0 and fp == 0 and fn == 0


def prf_with_empty_answer(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """``precision_recall_f1``, counting an empty-vs-empty comparison as correct."""
    if is_empty_answer(tp, fp, fn):
        return 1.0, 1.0, 1.0
    return precision_recall_f1(tp, fp, fn)


def pooled_f1_counts(tp: int, fp: int, fn: int) -> dict[str, int]:
    """The counts a run pools for an F1: ``2TP`` of ``2TP + FP + FN``.

    Summed over the run they give the F1 over every item of every entry. 0 of 0 is the
    empty answer, scored ``EMPTY_ANSWER_SCORE``.
    """
    return {POOLED_NUMERATOR: 2 * tp, POOLED_DENOMINATOR: 2 * tp + fp + fn}


__all__ = [
    "EMPTY_ANSWER_SCORE",
    "is_empty_answer",
    "pooled_f1_counts",
    "prf_with_empty_answer",
]
