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

"""Generic set/multiset diagnostics.

Produces the TP/FP/FN breakdown with deterministically ordered ``missing`` and
``spurious`` **tuples**. Both comparisons need **hashable** elements (``set`` /
``Counter``).

``_sort_key`` orders only ``None``, ``bool``, finite ``int``, finite ``float``, ``str`` and
``tuple``\\ s of those; ``NaN``, ``±inf`` and any other type raise ``TypeError``. Non-JSON
values in ``missing``/``spurious`` must be normalized before going into a ``ScoreResult``.
"""

from collections import Counter
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from typing import Any

# Cross-type ranks: values of different types are ordered by rank, never by payload.
# Ints and floats get distinct ranks so a large int is never lossily coerced to float
# and ``1`` never collides with ``1.0``.
_NULL_RANK = 0
_BOOL_RANK = 1
_INT_RANK = 2
_FLOAT_RANK = 3
_STR_RANK = 4
_TUPLE_RANK = 5


def _sort_key(item: object) -> tuple[Any, ...]:
    """Stable, comparable ordering key for the closed set of supported value shapes.

    Supports ``None``, ``bool``, finite ``int``, finite ``float`` (``float.hex()``
    tie-break so ``-0.0``/``0.0`` order deterministically), ``str`` and ``tuple``\\ s of
    those. Anything else, ``NaN`` and ``±inf`` included, raises ``TypeError`` rather than
    ordering by ``repr()`` or identity: the caller normalizes such values first.
    """
    if item is None:
        return (_NULL_RANK,)
    if isinstance(item, bool):
        return (_BOOL_RANK, item)
    if isinstance(item, int):
        return (_INT_RANK, item)
    if isinstance(item, float):
        if item != item or item in (float("inf"), float("-inf")):
            raise TypeError(f"cannot deterministically order non-finite float {item!r}")
        return (_FLOAT_RANK, item, item.hex())
    if isinstance(item, str):
        return (_STR_RANK, item)
    if isinstance(item, tuple):
        return (_TUPLE_RANK, tuple(_sort_key(element) for element in item))
    raise TypeError(
        f"cannot deterministically order value of type {type(item).__name__!r}"
    )


def _validate_count(name: str, value: int) -> None:
    """Reject booleans, non-integers, and negative values for a diagnostic count."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be a non-boolean int, got {type(value).__name__!r}"
        )
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")


@dataclass(frozen=True, slots=True)
class DiagnosticSet[T]:
    """Immutable canonical TP/FP/FN breakdown surfaced by set-based metrics.

    ``tp``/``fp``/``fn`` are non-negative, non-boolean integers; ``missing`` and
    ``spurious`` are tuples of ``T`` in deterministic order. No JSON promise for arbitrary
    ``T``: callers MUST serialize them (see ``as_detail``) before a ``ScoreResult``.
    """

    tp: int
    fp: int
    fn: int
    missing: tuple[T, ...] = ()
    spurious: tuple[T, ...] = ()

    def __post_init__(self) -> None:
        _validate_count("tp", self.tp)
        _validate_count("fp", self.fp)
        _validate_count("fn", self.fn)
        if not isinstance(self.missing, tuple):
            object.__setattr__(self, "missing", tuple(self.missing))
        if not isinstance(self.spurious, tuple):
            object.__setattr__(self, "spurious", tuple(self.spurious))

    def as_detail(self) -> dict[str, object]:
        """Return a detached mapping suitable for embedding in ``ScoreResult.detail``.

        Returns:
            A new dict with ``tp``/``fp``/``fn`` and ``missing``/``spurious`` as fresh lists.
            The caller ensures the ``T`` values are JSON-serializable.
        """
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "missing": list(self.missing),
            "spurious": list(self.spurious),
        }


def set_diagnostics[T: Hashable](
    reference: Iterable[T], prediction: Iterable[T]
) -> DiagnosticSet[T]:
    """Compare two collections as sets (duplicates collapsed).

    Args:
        reference: The ground-truth items; must be hashable.
        prediction: The predicted items; must be hashable.

    Returns:
        The TP/FP/FN breakdown with deterministically ordered diagnostics.

    Raises:
        TypeError: If an element is unhashable, or ``_sort_key`` cannot order it.
    """
    ref, pred = set(reference), set(prediction)
    missing: tuple[T, ...] = tuple(sorted(ref - pred, key=_sort_key))
    spurious: tuple[T, ...] = tuple(sorted(pred - ref, key=_sort_key))
    tp = len(ref & pred)
    return DiagnosticSet(
        tp=tp, fp=len(spurious), fn=len(missing), missing=missing, spurious=spurious
    )


def multiset_diagnostics[T: Hashable](
    reference: Iterable[T], prediction: Iterable[T]
) -> DiagnosticSet[T]:
    """Compare two collections as multisets (duplicates are meaningful).

    Args:
        reference: The ground-truth items; must be hashable.
        prediction: The predicted items; must be hashable.

    Returns:
        The TP/FP/FN breakdown, where repeated items count separately.

    Raises:
        TypeError: If an element is unhashable, or ``_sort_key`` cannot order it.
    """
    ref, pred = Counter(reference), Counter(prediction)
    missing: tuple[T, ...] = tuple(sorted((ref - pred).elements(), key=_sort_key))
    spurious: tuple[T, ...] = tuple(sorted((pred - ref).elements(), key=_sort_key))
    tp = sum((ref & pred).values())
    return DiagnosticSet(
        tp=tp, fp=len(spurious), fn=len(missing), missing=missing, spurious=spurious
    )


__all__ = [
    "DiagnosticSet",
    "multiset_diagnostics",
    "set_diagnostics",
]
