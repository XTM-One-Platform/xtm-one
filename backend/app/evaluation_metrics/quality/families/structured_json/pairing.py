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

"""STRUCTURED_JSON — pairing the entries of two lists of objects.

An agent may return the same findings in another order, so entries are paired before
their fields are scored: by optimal assignment over the credit each pair earns, so the
pairing gives the most credit any pairing can, whatever the order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "MAX_ENTRY_PRODUCT",
    "MAX_PAIRS_SCORED",
    "EntryPairing",
    "entry_product_exceeded",
    "pair_entries",
]

# Not caller-configurable. Pairing scores every reference entry against every predicted
# one, and the agent under evaluation chooses the prediction length, so the work must stay
# bounded. A caller-supplied budget would make whether a score exists depend on something
# other than the inputs, as `prose_rouge_l` also states. Both bounds are inclusive and
# decide `scored` versus `skipped`, so changing one is a versioned contract change.
MAX_ENTRY_PRODUCT = 10_000
# The entry pairs one answer may score in all, lists nested in entries included.
MAX_PAIRS_SCORED = 100_000


@dataclass(frozen=True, slots=True)
class EntryPairing:
    """The outcome of pairing two lists of entries.

    ``unmatched_reference`` is reported as missing and ``unmatched_prediction`` as extra.
    Neither is dropped: a short answer that omits the hard entries must not score better
    than one that attempts them and gets them wrong.
    """

    pairs: tuple[tuple[int, int], ...]
    unmatched_reference: tuple[int, ...]
    unmatched_prediction: tuple[int, ...]


def entry_product_exceeded(reference_count: int, prediction_count: int) -> bool:
    """Whether pairing these two lists would exceed the compute bound."""
    return reference_count * prediction_count > MAX_ENTRY_PRODUCT


def pair_entries(
    credit: Sequence[Sequence[float]], reference_count: int, prediction_count: int
) -> EntryPairing:
    """Pair reference entries to predicted ones for the most credit in total.

    Args:
        credit: ``credit[i][j]`` is what reference entry ``i`` paired with predicted entry
            ``j`` earns.
        reference_count: The number of reference entries.
        prediction_count: The number of predicted entries.

    Returns:
        The :class:`EntryPairing`: ``min(reference_count, prediction_count)`` pairs.
    """
    pairs: tuple[tuple[int, int], ...] = ()
    if reference_count and prediction_count:
        from scipy.optimize import linear_sum_assignment

        rows, columns = linear_sum_assignment(credit, maximize=True)
        pairs = tuple(sorted(zip(rows.tolist(), columns.tolist(), strict=True)))
    paired_reference = {reference for reference, _ in pairs}
    paired_prediction = {prediction for _, prediction in pairs}
    return EntryPairing(
        pairs=pairs,
        unmatched_reference=tuple(
            i for i in range(reference_count) if i not in paired_reference
        ),
        unmatched_prediction=tuple(
            j for j in range(prediction_count) if j not in paired_prediction
        ),
    )
