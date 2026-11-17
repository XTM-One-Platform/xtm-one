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

"""STRUCTURED_JSON — per-field JSON scoring against a dataset-owned schema.

Each leaf field is scored in ``[0, 1]`` under a mode derived from its own schema
declaration, and the leaves are aggregated as a weighted mean:

- a nested object counts as its leaves, whether the answer holds it or not;
- a list of objects is paired entry to entry (``pairing``) and each entry counts as its
  leaves: a missing entry as its reference leaves, missed, an extra one as its leaves,
  extra. Objects of several shapes (an ``anyOf`` / ``oneOf`` of objects) are a list of
  objects too: a pair is scored under the shape its reference entry matches;
- a field the reference holds and the answer omits is a miss, ``required`` or not
  (``required`` is validity's concern); an empty value (``null``, ``[]``, ``{}``) on one
  side only is left out: nothing was expected, or nothing given.

Per-field scores rather than TP/FP/FN counts: integer buckets cannot give partial credit,
would count one wrong field as both a false positive and a false negative, and would
split a set-valued field into per-index leaves before its mode is known.

Aggregation is ``100 x sum(score x weight) / sum(weight)`` over the scored leaves; a
container's weight multiplies its leaves'. A leaf with ``score=None`` is in neither sum.
The two sums are the entry's pooled counts, so a run reads the weighted share of
every field of every entry, not the mean of its entries' shares.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.evaluation_metrics.quality.families.structured_json.field_modes import (
    FieldMode,
    FieldScore,
    FieldStatus,
    entry_schema,
    resolve_mode,
    resolve_schema,
    score_value,
)
from app.evaluation_metrics.quality.families.structured_json.pairing import (
    MAX_ENTRY_PRODUCT,
    MAX_PAIRS_SCORED,
    entry_product_exceeded,
    pair_entries,
)
from app.evaluation_metrics.quality.families.structured_json.pointer import child_pointer
from app.evaluation_metrics.quality.normalization.policies import ComparisonPolicy
from app.evaluation_metrics.quality.primitives.empty_answer import EMPTY_ANSWER_SCORE
from app.evaluation_metrics.quality.results import (
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    JSONValue,
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)
from app.evaluation_metrics.quality.unreadable import unreadable_answer

_META = MetricMetadata(
    key="json.field_accuracy",
    label="Field Accuracy",
    kind="accuracy",
    primary_measure="accuracy",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)

# A key one side does not hold, as opposed to one holding ``null``.
_ABSENT: Final = object()

_Scored = tuple[list[FieldScore], list[JSONValue]]

# The deepest field the walk descends to, in JSON Pointer tokens. The walk recurses
# once per level, and the agent under evaluation chooses how deep its answer nests: about
# 500 levels raised RecursionError and the entry was recorded as the agent's failure, its
# answer lost. Past the bound the metric is skipped with its reason, like the pairing
# bounds: a limit of ours, not the model's failure. Inclusive, not caller-configurable.
MAX_FIELD_DEPTH: Final = 64


class _TooDeepError(Exception):
    """Raised internally when the walk would descend past ``MAX_FIELD_DEPTH``."""

    def __init__(self, depth: int) -> None:
        super().__init__("field nesting too deep")
        self.depth = depth


def _check_depth(pointer: str) -> None:
    # An escaped reference token never holds ``/`` (RFC 6901), so the slashes count the levels.
    depth = pointer.count("/")
    if depth > MAX_FIELD_DEPTH:
        raise _TooDeepError(depth)


class _PairingTooLargeError(Exception):
    """Raised internally when pairing would exceed a compute bound."""

    def __init__(self, reference_count: int, prediction_count: int, pairs_scored: int) -> None:
        super().__init__("pairing input too large")
        self.reference_count = reference_count
        self.prediction_count = prediction_count
        self.pairs_scored = pairs_scored


@dataclass(slots=True)
class _Budget:
    """The entry pairs scored so far in one answer (``MAX_PAIRS_SCORED``), and the whole
    schema a local ``$ref`` resolves against."""

    root: Mapping[str, Any] | None
    pairs_scored: int = 0


def json_field_accuracy(
    reference: object,
    prediction: object,
    policy: ComparisonPolicy,
    schema: Mapping[str, Any] | None = None,
) -> ScoreResult:
    """Score ``prediction`` field by field against ``reference``.

    Args:
        reference: The already-parsed reference JSON value.
        prediction: The already-parsed predicted JSON value.
        policy: The comparison policy, keyed by JSON Pointer.
        schema: The dataset-owned schema describing the expected answer. Without it no
            mode can be derived and every field is scored ``exact``. ``routing`` always
            supplies it.

    Returns:
        ``scored`` with the weighted per-field mean on a 0-100 scale; ``EMPTY_ANSWER_SCORE``
        when nothing is expected and nothing given; ``scored``/0 when the answer
        has too many entries to pair; ``skipped`` when the reference alone exceeds the
        pairing bound, either side nests past ``MAX_FIELD_DEPTH``, the author excluded
        what was left to compare, or every field is empty on one side only.
    """
    if _is_empty(reference) and _is_empty(prediction):
        return _empty_answer([], [])

    try:
        fields, pairings = _score_node(
            reference, prediction, schema, policy, "", 1.0, _Budget(root=schema)
        )
    except _TooDeepError as exc:
        return _skipped(
            "nesting_too_deep", {"depth": exc.depth, "max_depth": MAX_FIELD_DEPTH}
        )
    except _PairingTooLargeError as exc:
        sizes: dict[str, JSONValue] = {
            "reference_entries": exc.reference_count,
            "prediction_entries": exc.prediction_count,
            "entry_product": exc.reference_count * exc.prediction_count,
            "max_entry_product": MAX_ENTRY_PRODUCT,
            "pairs_scored": exc.pairs_scored,
            "max_pairs_scored": MAX_PAIRS_SCORED,
        }
        if prediction is not reference:
            # The reference paired with itself: what an answer shaped like it costs.
            expected = json_field_accuracy(reference, reference, policy, schema)
            if expected.detail.get("reason") != "pairing_input_too_large":
                # It would have fit: the answer's fault.
                return unreadable_answer(
                    expected, {"reason": "prediction_too_many_entries", **sizes}
                )
        return _skipped("pairing_input_too_large", sizes)

    scorable = [field for field in fields if field.score is not None]
    total_weight = sum(field.weight for field in scorable)

    if not scorable or total_weight == 0:
        if any(_author_excluded(field, policy) for field in fields):
            # The author's choice: neither 100.0 (perfection while checking nothing) nor
            # 0.0 (blaming the model for it).
            return _skipped("no_scorable_fields", _breakdown(fields, pairings))
        if _holds_nothing(reference) and _holds_nothing(prediction):
            return _empty_answer(fields, pairings)
        # Each field empty on one side only: left out, so nothing compares.
        return _skipped("empty_on_one_side", _breakdown(fields, pairings))

    credit = _credit(scorable)
    return ScoreResult(
        status=MetricStatus.scored,
        value=100.0 * credit / total_weight,
        detail={
            **_breakdown(fields, pairings),
            POOLED_NUMERATOR: credit,
            POOLED_DENOMINATOR: total_weight,
        },
        metadata=_META,
    )


# --- Traversal -------------------------------------------------------------------------


def _score_node(
    reference: object,
    prediction: object,
    schema: Mapping[str, Any] | None,
    policy: ComparisonPolicy,
    pointer: str,
    factor: float,
    budget: _Budget,
) -> _Scored:
    """The leaves of one node; either side may be ``_ABSENT``. ``factor`` is the weight
    the node's containers pass down."""
    _check_depth(pointer)
    mode = resolve_mode(pointer, schema, policy, budget.root)
    weight = factor * policy.field_weight(pointer)

    if policy.is_ignored(pointer) or mode is FieldMode.excluded:
        return [FieldScore(pointer, mode, None, weight, "excluded")], []
    if prediction is _ABSENT:
        return _leaves(reference, schema, policy, pointer, factor, "missing", budget.root), []
    if reference is _ABSENT or (reference is None and _is_container(prediction)):
        return _leaves(prediction, schema, policy, pointer, factor, "extra", budget.root), []

    if _scored_whole(reference, pointer, policy) or _scored_whole(prediction, pointer, policy):
        score = score_value(mode, reference, prediction, pointer, policy)
        return [FieldScore(pointer, mode, score, weight, "matched")], []
    if isinstance(reference, Mapping) and isinstance(prediction, Mapping):
        return _score_object(reference, prediction, schema, policy, pointer, weight, budget)
    if isinstance(reference, Mapping):
        return _leaves(reference, schema, policy, pointer, factor, "missing", budget.root), []

    if mode is FieldMode.object_list and _is_list(reference):
        if _is_list(prediction):
            return _score_object_list(
                reference, prediction, schema, policy, pointer, weight, budget
            )
        return _leaves(reference, schema, policy, pointer, factor, "missing", budget.root), []

    score = score_value(mode, reference, prediction, pointer, policy)
    return [FieldScore(pointer, mode, score, weight, "matched")], []


def _score_object(
    reference: Mapping[str, Any],
    prediction: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    policy: ComparisonPolicy,
    pointer: str,
    weight: float,
    budget: _Budget,
) -> _Scored:
    properties = _properties(schema, budget.root, reference)
    fields: list[FieldScore] = []
    pairings: list[JSONValue] = []
    for key in sorted(set(reference) | set(prediction)):
        child_fields, child_pairings = _score_node(
            reference.get(key, _ABSENT),
            prediction.get(key, _ABSENT),
            properties.get(key),
            policy,
            child_pointer(pointer, key),
            weight,
            budget,
        )
        fields.extend(child_fields)
        pairings.extend(child_pairings)
    return fields, pairings


def _score_object_list(
    reference: Sequence[Any],
    prediction: Sequence[Any],
    schema: Mapping[str, Any] | None,
    policy: ComparisonPolicy,
    pointer: str,
    weight: float,
    budget: _Budget,
) -> _Scored:
    """Pair entries for the most credit, then count each entry as its leaves."""
    reference_count, prediction_count = len(reference), len(prediction)
    budget.pairs_scored += reference_count * prediction_count
    if (
        entry_product_exceeded(reference_count, prediction_count)
        or budget.pairs_scored > MAX_PAIRS_SCORED
    ):
        raise _PairingTooLargeError(reference_count, prediction_count, budget.pairs_scored)
    if not reference_count and not prediction_count:
        return [FieldScore(pointer, FieldMode.object_list, 1.0, weight, "matched")], []

    root = budget.root
    items = _item_schema(schema, root)
    # A pair is scored under the shape its reference entry matches, an unpaired entry
    # under its own.
    reference_schemas = [entry_schema(items, entry, root) for entry in reference]
    prediction_schemas = [entry_schema(items, entry, root) for entry in prediction]

    # Each entry is scored at its reference index, the pointer overrides are keyed by.
    def score_pair(reference_index: int, prediction_index: int, spent: _Budget) -> _Scored:
        return _score_node(
            reference[reference_index],
            prediction[prediction_index],
            reference_schemas[reference_index],
            policy,
            child_pointer(pointer, reference_index),
            weight,
            spent,
        )

    # Only each pair's credit and counted weight are kept; the chosen pairs are scored
    # again for their fields, so memory stays at one entry's fields.
    stats = [
        [_stats(score_pair(i, j, budget)[0]) for j in range(prediction_count)]
        for i in range(reference_count)
    ]
    missed = [
        _leaves(
            entry,
            reference_schemas[index],
            policy,
            child_pointer(pointer, index),
            weight,
            "missing",
            root,
        )
        for index, entry in enumerate(reference)
    ]
    # An extra entry stands for no reference entry, so no override keyed by a reference
    # index applies to it; it is reported after them, so no pointer is shared.
    unkeyed = ComparisonPolicy(prose_policy=policy.prose_policy)
    extra_weights = [
        _weight(
            _leaves(
                entry,
                prediction_schemas[index],
                unkeyed,
                child_pointer(pointer, reference_count),
                weight,
                "extra",
                root,
            )
        )
        for index, entry in enumerate(prediction)
    ]
    pairing = pair_entries(
        _pairing_objective(stats, [_weight(fields) for fields in missed], extra_weights),
        reference_count,
        prediction_count,
    )

    fields: list[FieldScore] = []
    pairings: list[JSONValue] = []
    for reference_index, prediction_index in pairing.pairs:
        entry_fields, entry_pairings = score_pair(reference_index, prediction_index, _Budget(root=root))
        fields.extend(entry_fields)
        pairings.extend(entry_pairings)
    for reference_index in pairing.unmatched_reference:
        fields.extend(missed[reference_index])
    for rank, prediction_index in enumerate(pairing.unmatched_prediction):
        fields.extend(
            _leaves(
                prediction[prediction_index],
                prediction_schemas[prediction_index],
                unkeyed,
                child_pointer(pointer, reference_count + rank),
                weight,
                "extra",
                root,
            )
        )
    pairings.append(
        {
            "pointer": _normalize_root(pointer),
            "pairs": [list(pair) for pair in pairing.pairs],
            "unmatched_reference": list(pairing.unmatched_reference),
            "unmatched_prediction": list(pairing.unmatched_prediction),
        }
    )
    return fields, pairings


def _pairing_objective(
    stats: Sequence[Sequence[tuple[float, float]]],
    missed: Sequence[float],
    extra: Sequence[float],
) -> list[list[float]]:
    """Credit first; among pairings with equal credit, the one counting fewest fields.

    Pairing two entries counts their pair's fields instead of each entry's own, so the
    pairing decides the denominator too. Its saving breaks credit ties only: scaled below
    1e-9, it never outweighs a credit difference, so the score does not depend on order.
    """
    savings = [
        [missed[i] + extra[j] - counted for j, (_, counted) in enumerate(row)]
        for i, row in enumerate(stats)
    ]
    scale = 1e-9 / (1.0 + max((abs(saving) for row in savings for saving in row), default=0.0))
    return [
        [credit + scale * saving for (credit, _), saving in zip(row, saved, strict=True)]
        for row, saved in zip(stats, savings, strict=True)
    ]


def _leaves(
    value: object,
    schema: Mapping[str, Any] | None,
    policy: ComparisonPolicy,
    pointer: str,
    factor: float,
    status: FieldStatus,
    root: Mapping[str, Any] | None,
) -> list[FieldScore]:
    """The leaves of a value only one side holds, each scored 0 as ``status``."""
    _check_depth(pointer)
    mode = resolve_mode(pointer, schema, policy, root)
    weight = factor * policy.field_weight(pointer)
    if policy.is_ignored(pointer) or mode is FieldMode.excluded or _is_empty(value):
        # An empty value on one side: nothing was expected, or nothing given.
        return [FieldScore(pointer, mode, None, weight, "excluded")]
    if _scored_whole(value, pointer, policy):
        return [FieldScore(pointer, mode, 0.0, weight, status)]
    if isinstance(value, Mapping):
        properties = _properties(schema, root, value)
        return [
            leaf
            for key in sorted(value)
            for leaf in _leaves(
                value[key],
                properties.get(key),
                policy,
                child_pointer(pointer, key),
                weight,
                status,
                root,
            )
        ]
    if mode is FieldMode.object_list and _is_list(value):
        items = _item_schema(schema, root)
        return [
            leaf
            for index, entry in enumerate(value)
            for leaf in _leaves(
                entry,
                entry_schema(items, entry, root),
                policy,
                child_pointer(pointer, index),
                weight,
                status,
                root,
            )
        ]
    return [FieldScore(pointer, mode, 0.0, weight, status)]


def _credit(fields: Sequence[FieldScore]) -> float:
    return sum(field.score * field.weight for field in fields if field.score is not None)


def _weight(fields: Sequence[FieldScore]) -> float:
    return sum(field.weight for field in fields if field.score is not None)


def _stats(fields: Sequence[FieldScore]) -> tuple[float, float]:
    return _credit(fields), _weight(fields)


def _scored_whole(value: object, pointer: str, policy: ComparisonPolicy) -> bool:
    """An object the author gave a mode is scored whole, under it."""
    mode = policy.field_mode(pointer)
    return (
        isinstance(value, Mapping)
        and mode is not None
        and mode not in (FieldMode.excluded, FieldMode.object_list)
    )


# --- Reporting -------------------------------------------------------------------------


def _breakdown(
    fields: list[FieldScore], pairings: list[JSONValue]
) -> dict[str, JSONValue]:
    """Build the ``detail`` payload.

    The five status lists (``correct``, ``incorrect``, ``missing``, ``extra``,
    ``excluded``) partition the pointers in ``per_field``: every reported field appears in
    exactly one. There is no ``tp``/``fp``/``fn``: integer buckets cannot express partial
    credit and would contradict the per-field scores.
    """
    by_status: dict[str, list[str]] = {
        "correct": [],
        "incorrect": [],
        "missing": [],
        "extra": [],
        "excluded": [],
    }
    per_field: list[JSONValue] = []

    for field in sorted(fields, key=lambda f: f.pointer):
        pointer = _normalize_root(field.pointer)
        bucket = (
            ("correct" if field.score == 1.0 else "incorrect")
            if field.status == "matched"
            else field.status
        )
        by_status[bucket].append(pointer)
        per_field.append(
            {
                "pointer": pointer,
                "mode": field.mode.value,
                "score": field.score,
                "weight": field.weight,
                "status": field.status,
            }
        )

    detail: dict[str, JSONValue] = {
        "missing": by_status["missing"],
        "extra": by_status["extra"],
        "incorrect": by_status["incorrect"],
        "correct": by_status["correct"],
        "excluded": by_status["excluded"],
        "exact_object_match": not (
            by_status["missing"] or by_status["extra"] or by_status["incorrect"]
        ),
        "per_field": per_field,
    }
    if pairings:
        detail["pairings"] = list(pairings)
    return detail


def _empty_answer(fields: list[FieldScore], pairings: list[JSONValue]) -> ScoreResult:
    """Nothing expected and nothing given: correct (``empty_answer``)."""
    return ScoreResult(
        status=MetricStatus.scored,
        value=EMPTY_ANSWER_SCORE,
        detail={
            **_breakdown(fields, pairings),
            "empty_answer": True,
            POOLED_NUMERATOR: 0,
            POOLED_DENOMINATOR: 0,
        },
        metadata=_META,
    )


def _author_excluded(field: FieldScore, policy: ComparisonPolicy) -> bool:
    return (
        field.mode is FieldMode.excluded or policy.is_ignored(field.pointer) or field.weight == 0
    )


def _skipped(reason: str, extra: Mapping[str, JSONValue]) -> ScoreResult:
    detail: dict[str, JSONValue] = {"reason": reason}
    detail.update(extra)
    return ScoreResult(
        status=MetricStatus.skipped, value=None, detail=detail, metadata=_META
    )


# --- Helpers ---------------------------------------------------------------------------


def _properties(
    schema: Mapping[str, Any] | None, root: Mapping[str, Any] | None, value: object
) -> Mapping[str, Any]:
    """The properties ``value`` is scored under: of its shape, or of the branch it matches
    when the object is declared of several shapes."""
    schema = entry_schema(schema, value, root)
    if not isinstance(schema, Mapping):
        return {}
    declared = schema.get("properties")
    return declared if isinstance(declared, Mapping) else {}


def _item_schema(
    schema: Mapping[str, Any] | None, root: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    schema = resolve_schema(schema, root)
    declared = resolve_schema(schema.get("items"), root) if isinstance(schema, Mapping) else None
    return declared if isinstance(declared, Mapping) else None


def _is_container(value: object) -> bool:
    return isinstance(value, Mapping) or _is_list(value)


def _is_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, (Mapping, list, tuple)):
        return len(value) == 0
    return False


def _holds_nothing(value: object) -> bool:
    """Empty, or a container of values that hold nothing: ``{"a": null, "b": []}``."""
    if isinstance(value, Mapping):
        return all(_holds_nothing(item) for item in value.values())
    if _is_list(value):
        return all(_holds_nothing(item) for item in value)
    return value is None


def _normalize_root(pointer: str) -> str:
    """Render the root leaf path as ``/`` instead of the empty string."""
    return pointer or "/"


__all__ = ["MAX_FIELD_DEPTH", "json_field_accuracy"]
