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

"""The statistics of a placebo comparison (level 3).

``dev-docs/agent-placebo-evaluation.md`` FR-22 to FR-27, after the skill-placebo
method: per agent x model x target, the quality difference ``D`` and the token
ratio ``R`` of the real arm against its placebo (and of each against the
no-target arm), 95 % intervals and two-sided p-values from a cluster bootstrap
over the dataset entries, Holm's correction across the targets of one agent x
model, and the verdict.

An arm's quality is the evaluation's Overall Quality, rolled up as
``aggregation.reduce_grouped_metrics`` rolls a run up - a mean, pooled or macro
metric each as the suite declares it, with no spread penalty - then the
equal-weight mean of the selected metrics. Each metric is kept as per-entry
components (sums, counts, numerators, class counts), so a bootstrap resample is
a sum over the drawn entries, not a new roll-up.

Pure: it reads the trials it is handed and nothing else; the caller runs it off
the event loop.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from app.services.evaluation.metric_package import (
    HIGHER_IS_BETTER,
    MACRO_COUNT_KEYS,
    is_macro_key,
    is_pooled_key,
    metric_direction,
)

#: Bootstrap resamples (FR-24).
RESAMPLES = 10_000
#: Resamples drawn at once: bounds memory to a block times the entries.
_BLOCK = 1_000
#: Two-sided interval.
CONFIDENCE = 0.95
#: Threshold of the Holm-adjusted p-value a verdict reads (FR-26).
ALPHA = 0.05
#: Below this many entries a row warns; below the second, it has no verdict (FR-27).
FEW_ENTRIES = 10
MIN_ENTRIES_FOR_VERDICT = 3

_QUALITY_MAX = 100.0

VERDICT_BEATS = "beats_placebo"
VERDICT_TRADE_OFF = "trade_off"
VERDICT_WORSE = "worse_than_placebo"
VERDICT_NO_BETTER = "no_better_than_placebo"
VERDICT_NOT_COMPUTABLE = "not_computable"


@dataclass(frozen=True)
class MetricValue:
    """One quality metric of one result, as its ``evaluation_metrics`` row stores it."""

    value: float
    numerator: float | None = None
    denominator: float | None = None
    class_counts: Mapping[str, Mapping[str, int]] | None = None


@dataclass(frozen=True)
class Trial:
    """One counted result: one execution of one entry by one arm."""

    #: Quality metric key -> its value on this result.
    quality: Mapping[str, MetricValue]
    #: ``performance.tokens.total``; ``None`` when the run measured none.
    tokens: float | None
    judge: float | None = None
    latency: float | None = None
    tool_calls: float | None = None
    #: For a skill target: whether ``load_skill`` opened the target (or its placebo).
    skill_loaded: bool | None = None


#: Entry -> the trials of one arm on that entry.
Arm = Mapping[str, Sequence[Trial]]


@dataclass(frozen=True)
class Cell:
    """One agent x model x target of a comparison."""

    agent_id: str
    agent_name: str
    model_id: str
    target: dict
    target_key: str
    real: Arm
    placebo: Arm
    no_target: Arm | None = None
    #: Every run of the cell completed: otherwise there is no verdict (FR-27).
    complete: bool = True
    #: What limits the cell before any statistic: its placebo's own warnings.
    warnings: tuple[str, ...] = ()


@dataclass
class Effect:
    """An effect, its bootstrap interval and p-value; ``p_adjusted`` set by Holm."""

    estimate: float
    ci_low: float
    ci_high: float
    p: float
    p_adjusted: float | None = None

    def as_dict(self) -> dict:
        return {
            "estimate": round(self.estimate, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "p": round(self.p, 4),
            "p_adjusted": round(self.p_adjusted, 4) if self.p_adjusted is not None else None,
        }


@dataclass
class _Pair:
    """``first`` against ``second`` on both outcomes."""

    quality: Effect | None
    tokens: Effect | None
    entries: int
    quality_keys: list[str]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "quality": self.quality.as_dict() if self.quality else None,
            "tokens": self.tokens.as_dict() if self.tokens else None,
            "entries": self.entries,
            "quality_keys": self.quality_keys,
        }


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x1f".join(parts).encode()).digest()[:8], "big")


@dataclass(frozen=True)
class _Metric:
    """One quality metric of one arm as per-entry components.

    ``count`` is the arm's rows per entry; ``total`` the sum of the oriented
    values (mean), of the numerators (pooled) or of the raw values (macro, read
    when no class was seen); ``denominator`` and ``classes`` (entries x classes x
    tp/fp/fn) the pooled and macro counts.
    """

    kind: str
    oriented_high: bool
    count: np.ndarray
    total: np.ndarray
    denominator: np.ndarray | None = None
    classes: np.ndarray | None = None

    def values(self, draws: np.ndarray) -> np.ndarray:
        """The metric's 0-100 goodness on each resample (``draws``: resamples x entries); NaN when unmeasured."""
        count = self.count[draws].sum(axis=1)
        total = self.total[draws].sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            if self.kind == "mean":
                return np.where(count > 0, total / count, np.nan)
            if self.kind == "pooled":
                denominator = self.denominator[draws].sum(axis=1)
                # Every entry 0 of 0 is an empty answer, correct: full (``aggregation._pool``).
                score = np.where(denominator > 0, total / denominator, 1.0) * _QUALITY_MAX
            else:
                summed = self.classes[draws].sum(axis=1)
                tp, fp, fn = summed[..., 0], summed[..., 1], summed[..., 2]
                seen = (tp + fp + fn) > 0
                f1 = np.where(2 * tp + fp + fn > 0, 2 * tp / (2 * tp + fp + fn), 0.0)
                classes_seen = seen.sum(axis=-1)
                macro = np.where(
                    classes_seen > 0,
                    (f1 * seen).sum(axis=-1) / classes_seen,
                    total / count / _QUALITY_MAX,
                )
                score = macro * _QUALITY_MAX
            oriented = score if self.oriented_high else _QUALITY_MAX - score
            return np.where(count > 0, oriented, np.nan)


def _metric(arm: Arm, entries: Sequence[str], key: str) -> _Metric | None:
    """``key`` on ``entries`` of ``arm``, or ``None`` when a row cannot be rolled up.

    As ``reduce_grouped_metrics`` drops it: an unknown direction, a mean value
    outside 0-100, a pooled or macro row without its counts.
    """
    direction = metric_direction(key)
    if direction is None:
        return None
    high = direction == HIGHER_IS_BETTER
    kind = "macro" if is_macro_key(key) else "pooled" if is_pooled_key(key) else "mean"
    count = np.zeros(len(entries))
    total = np.zeros(len(entries))
    denominator = np.zeros(len(entries))
    rows: list[list[Mapping[str, Mapping[str, int]]]] = [[] for _ in entries]
    for index, entry in enumerate(entries):
        for trial in arm.get(entry, ()):
            metric = trial.quality.get(key)
            if metric is None:
                continue
            count[index] += 1
            if kind == "mean":
                if not 0.0 <= metric.value <= _QUALITY_MAX:
                    return None
                total[index] += metric.value if high else _QUALITY_MAX - metric.value
            elif kind == "pooled":
                if metric.numerator is None or metric.denominator is None:
                    return None
                total[index] += metric.numerator
                denominator[index] += metric.denominator
            else:
                if metric.class_counts is None:
                    return None
                total[index] += metric.value
                rows[index].append(metric.class_counts)
    if not count.any():
        return None
    if kind == "mean":
        return _Metric(kind, high, count, total)
    if kind == "pooled":
        return _Metric(kind, high, count, total, denominator=denominator)
    names = sorted({name for entry_rows in rows for classes in entry_rows for name in classes})
    classes = np.zeros((len(entries), max(len(names), 1), len(MACRO_COUNT_KEYS)))
    position = {name: column for column, name in enumerate(names)}
    for index, entry_rows in enumerate(rows):
        for row in entry_rows:
            for name, counts in row.items():
                classes[index, position[name]] += [counts.get(part, 0) for part in ("tp", "fp", "fn")]
    return _Metric(kind, high, count, total, classes=classes)


def _quality_keys(
    first: Arm, second: Arm, selection: Sequence[str] | None, default_keys: frozenset[str]
) -> list[str]:
    """The Overall Quality keys both arms measured: the selection, or the defaults when none is set."""
    present = set()
    for arm in (first, second):
        keys = {key for trials in arm.values() for trial in trials for key in trial.quality}
        present = keys if not present else present & keys
    wanted = default_keys if selection is None else set(selection)
    return sorted(present & wanted)


def _bootstrap(stat: Callable[[np.ndarray], np.ndarray], entries: int, *, null: float, seed: int) -> Effect | None:
    """The effect ``stat`` computes on all ``entries``, its 95 % interval and two-sided p-value.

    Each resample draws the entries with replacement and keeps every trial of
    both arms for each drawn entry (FR-24).
    """
    estimate = float(stat(np.arange(entries)[np.newaxis, :])[0])
    if not np.isfinite(estimate):
        return None
    rng = np.random.default_rng(seed)
    blocks = []
    for start in range(0, RESAMPLES, _BLOCK):
        draws = rng.integers(0, entries, size=(min(_BLOCK, RESAMPLES - start), entries))
        blocks.append(stat(draws))
    stats = np.concatenate(blocks)
    stats = stats[np.isfinite(stats)]
    if not len(stats):
        return None
    tail = (1 - CONFIDENCE) / 2 * 100
    low, high = np.percentile(stats, [tail, 100 - tail])
    below = (np.count_nonzero(stats <= null) + 1) / (len(stats) + 1)
    above = (np.count_nonzero(stats >= null) + 1) / (len(stats) + 1)
    return Effect(estimate=estimate, ci_low=float(low), ci_high=float(high), p=float(min(1.0, 2 * min(below, above))))


def _quality_stat(metrics: list[_Metric]) -> Callable[[np.ndarray], np.ndarray]:
    def _stat(draws: np.ndarray) -> np.ndarray:
        stacked = np.stack([metric.values(draws) for metric in metrics])
        counted = np.isfinite(stacked).sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(counted > 0, np.nansum(stacked, axis=0) / counted, np.nan)

    return _stat


def _compare(
    first: Arm,
    second: Arm,
    *,
    selection: Sequence[str] | None,
    default_keys: frozenset[str],
    seed: tuple[str, ...],
) -> _Pair:
    entries = sorted(entry for entry in first.keys() & second.keys() if first[entry] and second[entry])
    keys = _quality_keys(first, second, selection, default_keys)
    quality: Effect | None = None
    usable: list[str] = []
    if entries and keys:
        pairs = [(key, _metric(first, entries, key), _metric(second, entries, key)) for key in keys]
        usable = [key for key, a, b in pairs if a is not None and b is not None]
        if usable:
            stat_a = _quality_stat([a for _, a, b in pairs if a is not None and b is not None])
            stat_b = _quality_stat([b for _, a, b in pairs if a is not None and b is not None])
            quality = _bootstrap(
                lambda draws: stat_a(draws) - stat_b(draws), len(entries), null=0.0, seed=_seed(*seed, "quality")
            )
    pair = _Pair(quality=quality, tokens=None, entries=len(entries), quality_keys=usable)
    if quality is None:
        pair.warnings.append("quality_unmeasured")

    sums: list[tuple[float, int, float, int]] = []
    partial = False
    for entry in entries:
        row = []
        for arm in (first, second):
            measured = [trial.tokens for trial in arm[entry] if trial.tokens is not None]
            partial = partial or len(measured) < len(arm[entry])
            row.extend((float(sum(measured)), len(measured)))
        if row[1] and row[3]:
            sums.append(tuple(row))
    if sums:
        sum_a, n_a, sum_b, n_b = np.array(sums, dtype=float).T

        def _ratio(draws: np.ndarray) -> np.ndarray:
            with np.errstate(divide="ignore", invalid="ignore"):
                return (sum_a[draws].sum(1) / n_a[draws].sum(1)) / (sum_b[draws].sum(1) / n_b[draws].sum(1))

        pair.tokens = _bootstrap(_ratio, len(sums), null=1.0, seed=_seed(*seed, "tokens"))
    if pair.tokens is None:
        pair.warnings.append("tokens_unmeasured")
    elif partial:
        pair.warnings.append("tokens_partially_measured")
    return pair


def holm(p_values: Sequence[float | None]) -> list[float | None]:
    """Holm-adjusted p-values, in the input order; ``None`` stays ``None`` and is not counted."""
    indexed = sorted((p, i) for i, p in enumerate(p_values) if p is not None)
    adjusted: list[float | None] = [None] * len(p_values)
    running = 0.0
    for rank, (p, index) in enumerate(indexed):
        running = max(running, min(1.0, (len(indexed) - rank) * p))
        adjusted[index] = running
    return adjusted


def verdict(quality: Effect | None, tokens: Effect | None) -> tuple[str, bool]:
    """The real arm's verdict against its placebo, and whether it rests on tokens alone (FR-26).

    An outcome improves when its adjusted p is below ``ALPHA`` in the good
    direction (``D > 0``, ``R < 1``), worsens in the bad one.
    """

    def _moves(effect: Effect | None, better_when_high: bool) -> tuple[bool, bool]:
        if effect is None or effect.p_adjusted is None or effect.p_adjusted >= ALPHA:
            return False, False
        higher = effect.estimate > (0.0 if better_when_high else 1.0)
        return higher == better_when_high, higher != better_when_high

    quality_up, quality_down = _moves(quality, better_when_high=True)
    tokens_up, tokens_down = _moves(tokens, better_when_high=False)
    improves = quality_up or tokens_up
    worsens = quality_down or tokens_down
    if improves and worsens:
        return VERDICT_TRADE_OFF, False
    if improves:
        return VERDICT_BEATS, tokens_up and not quality_up
    if worsens:
        return VERDICT_WORSE, False
    return VERDICT_NO_BETTER, False


def _mean(values: Iterable[float | None]) -> float | None:
    measured = [float(value) for value in values if value is not None]
    return round(sum(measured) / len(measured), 4) if measured else None


def _arm_summary(arm: Arm) -> dict:
    """What an arm measured, plain means over its trials (FR-22's secondary measures)."""
    trials = [trial for entry_trials in arm.values() for trial in entry_trials]
    return {
        "entries": sum(1 for entry_trials in arm.values() if entry_trials),
        "trials": len(trials),
        "tokens": _mean(trial.tokens for trial in trials),
        "judge": _mean(trial.judge for trial in trials),
        "latency": _mean(trial.latency for trial in trials),
        "tool_calls": _mean(trial.tool_calls for trial in trials),
        "skill_loaded": _mean(
            float(trial.skill_loaded) if trial.skill_loaded is not None else None for trial in trials
        ),
    }


def comparison_statistics(
    cells: Sequence[Cell],
    *,
    seed: str,
    quality_selection: Sequence[str] | None,
    default_quality_keys: frozenset[str],
) -> dict:
    """A comparison's results: one row per agent x model x target (FR-23 to FR-27).

    ``quality_selection`` is the evaluation's ``overall_quality_metrics`` (``None``:
    the family's ``default_quality_keys``; ``[]``: no quality, so no verdict).
    """
    compare = {"selection": quality_selection, "default_keys": default_quality_keys}
    rows: list[dict] = []
    against_placebo: list[_Pair] = []
    for cell in cells:
        key = (seed, cell.agent_id, cell.model_id, cell.target_key)
        vs_placebo = _compare(cell.real, cell.placebo, seed=(*key, "placebo"), **compare)
        against_placebo.append(vs_placebo)
        arms = {"real": _arm_summary(cell.real), "placebo": _arm_summary(cell.placebo)}
        row = {
            "agent_id": cell.agent_id,
            "agent_name": cell.agent_name,
            "model_id": cell.model_id,
            "target": cell.target,
            "target_key": cell.target_key,
            "entries": vs_placebo.entries,
            "arms": arms,
            "warnings": list(dict.fromkeys([*cell.warnings, *vs_placebo.warnings])),
            "vs_placebo": None,
            "vs_no_target": None,
            "placebo_vs_no_target": None,
        }
        if cell.no_target is not None:
            arms["no_target"] = _arm_summary(cell.no_target)
            row["vs_no_target"] = _compare(
                cell.real, cell.no_target, seed=(*key, "real-no-target"), **compare
            ).as_dict()
            row["placebo_vs_no_target"] = _compare(
                cell.placebo, cell.no_target, seed=(*key, "placebo-no-target"), **compare
            ).as_dict()
        rows.append(row)

    # A cell gets a verdict when its runs all completed and it has the entries
    # and the quality to read (FR-27); only those are tested, so only those
    # enter Holm's correction across the targets of one agent x model (FR-25).
    tested = [
        cell.complete and pair.entries >= MIN_ENTRIES_FOR_VERDICT and pair.quality is not None
        for cell, pair in zip(cells, against_placebo, strict=True)
    ]
    groups: dict[tuple[str, str], list[int]] = {}
    for index, cell in enumerate(cells):
        if tested[index]:
            groups.setdefault((cell.agent_id, cell.model_id), []).append(index)
    for indexes in groups.values():
        for outcome in ("quality", "tokens"):
            effects = [getattr(against_placebo[index], outcome) for index in indexes]
            for effect, adjusted in zip(effects, holm([e.p if e else None for e in effects]), strict=True):
                if effect is not None:
                    effect.p_adjusted = adjusted

    for row, cell, pair, verdict_due in zip(rows, cells, against_placebo, tested, strict=True):
        row["vs_placebo"] = pair.as_dict()
        if not cell.complete:
            row["warnings"].append("run_incomplete")
        if verdict_due:
            row["verdict"], row["rests_on_tokens_alone"] = verdict(pair.quality, pair.tokens)
        else:
            row["verdict"], row["rests_on_tokens_alone"] = VERDICT_NOT_COMPUTABLE, False
        if pair.entries < FEW_ENTRIES:
            row["warnings"].append("few_entries")

    return {
        "method": {
            "resamples": RESAMPLES,
            "confidence": CONFIDENCE,
            "alpha": ALPHA,
            "multiple_comparisons": "holm",
            "quality_selection": list(quality_selection) if quality_selection is not None else None,
        },
        "rows": rows,
    }
