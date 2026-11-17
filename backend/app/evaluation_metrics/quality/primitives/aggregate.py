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

"""Macro aggregation over per-label diagnostics.

The macro universe is the labels of the per-label mapping, in sorted order: the caller
decides which labels count by which ones it diagnoses. An empty mapping follows the
zero-denominator policy.
"""

from collections.abc import Iterable, Mapping

from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import DiagnosticSet


def macro_average[T](
    per_label: Mapping[str, DiagnosticSet[T]],
) -> tuple[float, float, float]:
    """Average per-label P/R/F1 over the labels of ``per_label``.

    Args:
        per_label: Diagnostics keyed by label.

    Returns:
        The ``(precision, recall, f1)`` triple on a 0..1 scale; an empty mapping returns
        ``(0.0, 0.0, 0.0)``.
    """
    if not per_label:
        return 0.0, 0.0, 0.0
    p_sum = r_sum = f_sum = 0.0
    for label in sorted(per_label):
        diag = per_label[label]
        p, r, f = precision_recall_f1(diag.tp, diag.fp, diag.fn)
        p_sum += p
        r_sum += r
        f_sum += f
    n = len(per_label)
    return p_sum / n, r_sum / n, f_sum / n


def macro_f1_from_counts(classes: Iterable[Mapping[str, int]]) -> float:
    """The macro F1 of per-class counts (``{"tp", "fp", "fn"}`` each), on a 0..1 scale.

    The F1 :func:`macro_average` averages, from counts alone: an entry's per-class counts
    or a run's, summed. A class with no count on either side scores 0, as there;
    no class at all is ``0.0``, the zero-denominator policy, which a caller handling an
    empty answer decides before calling.
    """
    f1s = [precision_recall_f1(c["tp"], c["fp"], c["fn"])[2] for c in classes]
    return sum(f1s) / len(f1s) if f1s else 0.0


__all__ = ["macro_average", "macro_f1_from_counts"]
