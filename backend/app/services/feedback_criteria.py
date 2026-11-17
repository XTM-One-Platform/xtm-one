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

"""Feedback criteria: what a rating is scored on, and how the scores add up.

The one place that decides whether a criterion definition is acceptable, whether
a score fits its criterion, what a score is worth on the common [0, 1] scale,
and how the scores of one rating combine into its weighted score. The REST API
(``api/v1/feedback_criteria.py``), the rating endpoints (``api/v1/annotations.py``,
``api/v1/chat.py``) and the agent feedback summary (``services/annotations.py``)
all go through here, so a score is judged the same way wherever it enters.

The weighted score of one rating::

    sum(weight_c * normalized_c) / sum(weight_c)   over its scored, enabled criteria

- no criterion scored (or none configured): the thumbs, 1.0 or 0.0;
- one criterion scored: that criterion's normalized value (the weight cancels);
- scored criteria that all weigh zero: the thumbs again. A configuration whose
  enabled criteria all weigh zero is refused at write time, so that last case
  only arises from ratings given before a weight was lowered.
"""

from __future__ import annotations

import math
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from sqlalchemy import delete, select

from app.core.utils import slugify
from app.models.annotation import AnnotationScore
from app.models.feedback_criterion import FeedbackCriterion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CriterionType = Literal["categorical", "numeric", "scale"]
CRITERION_TYPES: tuple[str, ...] = get_args(CriterionType)

#: A categorical criterion offers between these many options.
MIN_OPTIONS = 2
MAX_OPTIONS = 20
#: A scale is drawn as one button per step, so it stays short.
MAX_SCALE_STEPS = 21
#: Histogram buckets of a numeric criterion (equal widths over its bounds).
NUMERIC_BUCKETS = 5
MAX_KEY_LENGTH = 64
MAX_OPTION_VALUE_LENGTH = 100


class CriterionError(ValueError):
    """A criterion definition or a score that cannot be accepted.

    The message is written for the person who sent it and names the criterion.
    """


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def validate_shape(
    criterion_type: str,
    options: Sequence[dict[str, Any]] | None,
    min_value: float | None,
    max_value: float | None,
) -> tuple[list[dict[str, Any]] | None, float | None, float | None]:
    """The type-specific part of a definition, cleaned, or ``CriterionError``.

    Returns ``(options, min_value, max_value)`` as they must be stored: a
    categorical criterion keeps its options and no bounds, a numeric or scale
    one keeps its bounds and no options.
    """
    if criterion_type not in CRITERION_TYPES:
        raise CriterionError(f"Unknown criterion type {criterion_type!r}.")

    if criterion_type == "categorical":
        if min_value is not None or max_value is not None:
            raise CriterionError("A categorical criterion has options, not a minimum or a maximum.")
        if not options or not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            raise CriterionError(
                f"A categorical criterion needs between {MIN_OPTIONS} and {MAX_OPTIONS} options."
            )
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, option in enumerate(options, start=1):
            value = str(option.get("value") or "").strip()
            label = str(option.get("label") or "").strip() or value
            score = option.get("score")
            if not value:
                raise CriterionError(f"Option {index} has no value.")
            if len(value) > MAX_OPTION_VALUE_LENGTH or len(label) > MAX_OPTION_VALUE_LENGTH:
                raise CriterionError(
                    f"Option {index} is longer than {MAX_OPTION_VALUE_LENGTH} characters."
                )
            if value.casefold() in seen:
                raise CriterionError(f"Option {value!r} appears twice.")
            if not _finite(score) or not (0 <= score <= 1):
                raise CriterionError(f"Option {value!r} needs a score between 0 and 1.")
            seen.add(value.casefold())
            cleaned.append({"value": value, "label": label, "score": float(score)})
        return cleaned, None, None

    if options:
        raise CriterionError(f"A {criterion_type} criterion has a minimum and a maximum, not options.")
    if not _finite(min_value) or not _finite(max_value):
        raise CriterionError(f"A {criterion_type} criterion needs a minimum and a maximum.")
    lo, hi = float(min_value), float(max_value)  # type: ignore[arg-type]
    if hi <= lo:
        raise CriterionError("The maximum must be greater than the minimum.")
    if criterion_type == "scale":
        if not lo.is_integer() or not hi.is_integer():
            raise CriterionError("A scale goes from one whole number to another (for example 1 to 5).")
        if hi - lo + 1 > MAX_SCALE_STEPS:
            raise CriterionError(f"A scale has at most {MAX_SCALE_STEPS} steps.")
    return None, lo, hi


def validate_weight(weight: Any) -> float:
    if not _finite(weight) or weight < 0:
        raise CriterionError("A weight is a number, zero or more.")
    return float(weight)


def assert_weights_usable(enabled_weights: Iterable[float]) -> None:
    """Refuse enabled criteria that all weigh zero: they would weigh nothing.

    *enabled_weights* is the weight of every criterion that will be enabled once
    the change is applied. No enabled criterion at all is fine (ratings fall back
    to the thumbs).
    """
    weights = list(enabled_weights)
    if weights and sum(weights) <= 0:
        raise CriterionError(
            "The enabled criteria would all weigh zero, so the weighted score would mean "
            "nothing. Give at least one of them a weight above zero."
        )


def base_key(name: str) -> str:
    """The key a criterion named *name* starts from (``accuracy``, ``tone-of-voice``).

    ``slugify`` answers "agent" for a name with no ASCII letter or digit (a
    Japanese name, say); such a criterion is keyed "criterion" instead.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    if not re.search(r"[A-Za-z0-9]", ascii_name):
        return "criterion"
    return slugify(name)[:MAX_KEY_LENGTH].strip("-") or "criterion"


def unique_key(base: str, taken: set[str]) -> str:
    """*base*, or *base* with the first free ``-2``, ``-3``... suffix."""
    if base not in taken:
        return base
    for suffix in range(2, len(taken) + 3):
        candidate = f"{base[: MAX_KEY_LENGTH - len(str(suffix)) - 1]}-{suffix}"
        if candidate not in taken:
            return candidate
    raise AssertionError("unreachable: more suffixes than taken keys")  # pragma: no cover


async def load_criteria(
    db: AsyncSession, *, include_disabled: bool = False
) -> list[FeedbackCriterion]:
    """The criteria in display order (sort_order, then name)."""
    query = select(FeedbackCriterion).order_by(
        FeedbackCriterion.sort_order, FeedbackCriterion.name, FeedbackCriterion.id
    )
    if not include_disabled:
        query = query.where(FeedbackCriterion.enabled.is_(True))
    return list((await db.execute(query)).scalars().all())


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedScore:
    criterion_id: uuid.UUID
    value_numeric: float | None
    value_text: str | None


def validate_scores(
    criteria: Sequence[FeedbackCriterion],
    raw_scores: Iterable[tuple[uuid.UUID, Any]],
) -> list[ValidatedScore]:
    """Every ``(criterion_id, value)`` checked against its criterion, or ``CriterionError``.

    A score must name an enabled criterion, at most once, with a value its type
    accepts: an option of a categorical criterion, a number within the bounds of
    a numeric one, a whole step of a scale.
    """
    by_id = {criterion.id: criterion for criterion in criteria if criterion.enabled}
    seen: set[uuid.UUID] = set()
    validated: list[ValidatedScore] = []
    for criterion_id, value in raw_scores:
        criterion = by_id.get(criterion_id)
        if criterion is None:
            raise CriterionError("A score names a criterion that does not exist or is disabled.")
        if criterion_id in seen:
            raise CriterionError(f"{criterion.name!r} is scored twice.")
        seen.add(criterion_id)
        if criterion.type == "categorical":
            options = {str(option["value"]) for option in criterion.options or []}
            if not isinstance(value, str) or value not in options:
                raise CriterionError(f"{criterion.name!r} takes one of its options.")
            validated.append(ValidatedScore(criterion_id, None, value))
            continue
        if not _finite(value):
            raise CriterionError(f"{criterion.name!r} takes a number.")
        lo, hi = criterion.min_value, criterion.max_value
        if lo is None or hi is None:
            raise CriterionError(f"{criterion.name!r} has no range to score against.")
        if not lo <= value <= hi:
            raise CriterionError(f"{criterion.name!r} takes a value from {lo:g} to {hi:g}.")
        if criterion.type == "scale" and not float(value).is_integer():
            raise CriterionError(f"{criterion.name!r} takes a whole step of its scale.")
        validated.append(ValidatedScore(criterion_id, float(value), None))
    return validated


def normalized_value(
    criterion: FeedbackCriterion, value_numeric: float | None, value_text: str | None
) -> float | None:
    """A stored score on the common [0, 1] scale; ``None`` when it no longer maps.

    A categorical score is its option's score (``None`` if the option was
    removed since); a numeric or scale score is its position between the bounds,
    clamped (bounds narrowed since the score was given).
    """
    if criterion.type == "categorical":
        for option in criterion.options or []:
            if str(option.get("value")) == value_text:
                score = option.get("score")
                return float(score) if _finite(score) else None
        return None
    lo, hi = criterion.min_value, criterion.max_value
    if value_numeric is None or lo is None or hi is None or hi <= lo:
        return None
    return min(1.0, max(0.0, (value_numeric - lo) / (hi - lo)))


def weighted_score(rating: str, scored: Iterable[tuple[float, float | None]]) -> float:
    """The weighted score of one rating, from its ``(weight, normalized)`` pairs.

    See the module docstring for the fallbacks to the thumbs.
    """
    pairs = [(weight, value) for weight, value in scored if value is not None]
    total = sum(weight for weight, _ in pairs)
    if total > 0:
        return sum(weight * value for weight, value in pairs) / total
    return 1.0 if rating == "positive" else 0.0


def rating_weighted_score(
    rating: str,
    scores: Iterable[AnnotationScore],
    criteria_by_id: dict[uuid.UUID, FeedbackCriterion],
) -> float:
    """``weighted_score`` of one rating from its stored scores (enabled criteria only)."""
    scored: list[tuple[float, float | None]] = []
    for score in scores:
        criterion = criteria_by_id.get(score.criterion_id)
        if criterion is None or not criterion.enabled:
            continue
        scored.append(
            (criterion.weight, normalized_value(criterion, score.value_numeric, score.value_text))
        )
    return weighted_score(rating, scored)


async def replace_scores(
    db: AsyncSession,
    *,
    annotation_id: uuid.UUID | None = None,
    message_feedback_id: uuid.UUID | None = None,
    scores: Sequence[ValidatedScore],
) -> None:
    """Make *scores* the whole set of scores of one rating (in the caller's transaction)."""
    if (annotation_id is None) == (message_feedback_id is None):
        raise ValueError("replace_scores needs exactly one of annotation_id / message_feedback_id")
    parent = (
        AnnotationScore.annotation_id == annotation_id
        if annotation_id is not None
        else AnnotationScore.message_feedback_id == message_feedback_id
    )
    await db.execute(delete(AnnotationScore).where(parent))
    for score in scores:
        db.add(
            AnnotationScore(
                annotation_id=annotation_id,
                message_feedback_id=message_feedback_id,
                criterion_id=score.criterion_id,
                value_numeric=score.value_numeric,
                value_text=score.value_text,
            )
        )
    await db.flush()


async def load_scores(
    db: AsyncSession,
    *,
    annotation_ids: Sequence[uuid.UUID] = (),
    message_feedback_ids: Sequence[uuid.UUID] = (),
) -> dict[uuid.UUID, list[AnnotationScore]]:
    """The scores of the given ratings, keyed by the rating's own id."""
    grouped: dict[uuid.UUID, list[AnnotationScore]] = {}
    queries = []
    if annotation_ids:
        queries.append(select(AnnotationScore).where(AnnotationScore.annotation_id.in_(annotation_ids)))
    if message_feedback_ids:
        queries.append(
            select(AnnotationScore).where(AnnotationScore.message_feedback_id.in_(message_feedback_ids))
        )
    for query in queries:
        for score in (await db.execute(query)).scalars().all():
            parent = score.annotation_id or score.message_feedback_id
            if parent is not None:
                grouped.setdefault(parent, []).append(score)
    return grouped


def score_value(score: AnnotationScore) -> float | str | None:
    """The value as it was given: an option, or a number."""
    return score.value_text if score.value_text is not None else score.value_numeric


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HistogramBucket:
    key: str
    label: str
    count: int = 0


@dataclass(slots=True)
class CriterionStatistics:
    criterion: FeedbackCriterion
    count: int = 0
    mean_score: float | None = None
    histogram: list[HistogramBucket] = field(default_factory=list)


def _buckets(criterion: FeedbackCriterion) -> list[HistogramBucket]:
    if criterion.type == "categorical":
        return [
            HistogramBucket(key=str(option["value"]), label=str(option.get("label") or option["value"]))
            for option in criterion.options or []
        ]
    lo, hi = criterion.min_value, criterion.max_value
    if lo is None or hi is None or hi <= lo:
        return []
    if criterion.type == "scale":
        return [
            HistogramBucket(key=f"{step:g}", label=f"{step:g}")
            for step in range(int(lo), int(hi) + 1)
        ]
    width = (hi - lo) / NUMERIC_BUCKETS
    return [
        HistogramBucket(
            key=str(index),
            label=f"{lo + index * width:g}-{lo + (index + 1) * width:g}",
        )
        for index in range(NUMERIC_BUCKETS)
    ]


def _bucket_index(
    criterion: FeedbackCriterion, value_numeric: float | None, value_text: str | None
) -> int | None:
    if criterion.type == "categorical":
        for index, option in enumerate(criterion.options or []):
            if str(option["value"]) == value_text:
                return index
        return None
    lo, hi = criterion.min_value, criterion.max_value
    if value_numeric is None or lo is None or hi is None or hi <= lo:
        return None
    clamped = min(hi, max(lo, value_numeric))
    if criterion.type == "scale":
        return round(clamped - lo)
    return min(NUMERIC_BUCKETS - 1, int((clamped - lo) / ((hi - lo) / NUMERIC_BUCKETS)))


def criterion_statistics(
    criterion: FeedbackCriterion,
    groups: Iterable[tuple[float | None, str | None, int]],
) -> CriterionStatistics:
    """Count, mean normalized score and histogram of one criterion, from
    ``(value_numeric, value_text, count)`` groups. A score that no longer maps
    (a removed option) is left out of all three."""
    stats = CriterionStatistics(criterion=criterion, histogram=_buckets(criterion))
    total = 0.0
    for value_numeric, value_text, count in groups:
        normalized = normalized_value(criterion, value_numeric, value_text)
        if normalized is None:
            continue
        stats.count += count
        total += normalized * count
        index = _bucket_index(criterion, value_numeric, value_text)
        if index is not None and index < len(stats.histogram):
            stats.histogram[index].count += count
    stats.mean_score = total / stats.count if stats.count else None
    return stats


def aggregate_weighted_score(
    statistics: Iterable[CriterionStatistics], positive: int, negative: int
) -> float | None:
    """An agent's weighted score: the weighted mean of its enabled criteria's means.

    Falls back to the share of thumbs up when no enabled criterion has a score,
    and is ``None`` when nothing was rated at all.
    """
    pairs = [
        (stats.criterion.weight, stats.mean_score)
        for stats in statistics
        if stats.criterion.enabled and stats.mean_score is not None
    ]
    total = sum(weight for weight, _ in pairs)
    if total > 0:
        return sum(weight * mean for weight, mean in pairs) / total
    rated = positive + negative
    return positive / rated if rated else None
