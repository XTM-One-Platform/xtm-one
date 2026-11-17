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

"""Precision / recall / F1 with strict integer-count validation.

Counts (``tp``/``fp``/``fn``) MUST be non-negative integers. Booleans are rejected even
though ``bool`` subclasses ``int``; non-integers raise ``TypeError``, negative counts
``ValueError``. These are programming errors, never a score.

A zero denominator yields ``0.0``, not ``NaN`` or an exception (``zero_division=0.0``).
Values are a ``tuple[float, float, float]`` on a 0..1 scale; callers scale to 0..100 for
``ScoreResult.value``. The tuple is the stable public contract.
"""


def _validate_count(name: str, value: int) -> None:
    """Reject booleans, non-integers, and negative values for a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be a non-boolean int, got {type(value).__name__!r}"
        )
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Return ``(precision, recall, f1)`` on a 0..1 scale.

    Args:
        tp: True positives (non-negative, non-boolean ``int``).
        fp: False positives (non-negative, non-boolean ``int``).
        fn: False negatives (non-negative, non-boolean ``int``).

    Returns:
        The ``(precision, recall, f1)`` triple, F1 computed from counts as
        ``2 * tp / (2 * tp + fp + fn)``. Each measure is ``0.0`` when its denominator is
        zero.

    Raises:
        TypeError: If any count is a ``bool`` or not an ``int``.
        ValueError: If any count is negative.
    """
    _validate_count("tp", tp)
    _validate_count("fp", fp)
    _validate_count("fn", fn)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1_denominator = 2 * tp + fp + fn
    f1 = (2 * tp / f1_denominator) if f1_denominator > 0 else 0.0
    return precision, recall, f1


__all__ = ["precision_recall_f1"]
