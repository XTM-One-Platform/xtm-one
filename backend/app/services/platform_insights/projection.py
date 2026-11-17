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

"""Month-over-month projection of a usage series: pure functions, no database.

``query_platform_analytics`` fits a series of COMPLETE months (the window's
first month when the window cuts it, the current month and any month before
the data the platform holds are left out) and projects it *horizon* months on:

* ``linear`` - the least-squares line through the months;
* ``cagr`` - the compound monthly growth from the first month to the last;
* ``last_n_avg`` - the average of the last *n* months, flat.

Every fit says how well it reads the months it was fitted on: ``r2`` (the
coefficient of determination, ``None`` for a flat average) and ``mape`` (the
mean absolute percentage error over the months with a value). A projected
value is never below zero: usage cannot be negative, and a negative trend
reads as "falls to zero" (``clamped``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

PROJECTION_METHODS: tuple[str, ...] = ("linear", "cagr", "last_n_avg")
#: The fewest complete months each method fits.
MIN_MONTHS: dict[str, int] = {"linear": 2, "cagr": 2, "last_n_avg": 1}
DEFAULT_LAST_N = 3


class ProjectionError(ValueError):
    """A series the method cannot fit, with the sentence that says why."""


@dataclass
class Fit:
    params: dict[str, float]
    fitted: list[float]
    projected: list[float]
    r2: float | None
    mape: float | None
    clamped: bool = False
    notes: list[str] = field(default_factory=list)


def add_months(month: date, count: int) -> date:
    """The first day of the month *count* months after *month*'s."""
    index = month.year * 12 + (month.month - 1) + count
    return date(index // 12, index % 12 + 1, 1)


def _r2(values: list[float], fitted: list[float]) -> float | None:
    mean = sum(values) / len(values)
    total = sum((value - mean) ** 2 for value in values)
    residual = sum((value - fit) ** 2 for value, fit in zip(values, fitted, strict=True))
    if total == 0:
        return 1.0 if residual == 0 else None
    return 1 - residual / total


def _mape(values: list[float], fitted: list[float]) -> float | None:
    errors = [abs(value - fit) / abs(value) for value, fit in zip(values, fitted, strict=True) if value]
    return 100 * sum(errors) / len(errors) if errors else None


def fit(method: str, values: list[float], *, horizon: int, last_n: int = DEFAULT_LAST_N) -> Fit:
    """Fit *values* (complete months, oldest first) with *method* and project *horizon* months."""
    if method not in PROJECTION_METHODS:
        raise ProjectionError(f"projection method must be one of {', '.join(PROJECTION_METHODS)}.")
    needed = MIN_MONTHS[method]
    if len(values) < needed:
        raise ProjectionError(
            f"{method} needs at least {needed} complete month(s) of data; this series has {len(values)}."
        )
    count = len(values)
    if method == "linear":
        mean_x = (count - 1) / 2
        mean_y = sum(values) / count
        spread = sum((index - mean_x) ** 2 for index in range(count))
        slope = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / spread
        intercept = mean_y - slope * mean_x
        fitted = [intercept + slope * index for index in range(count)]
        raw = [intercept + slope * (count - 1 + step) for step in range(1, horizon + 1)]
        result = Fit({"slope_per_month": slope, "intercept": intercept}, fitted, [], _r2(values, fitted), _mape(values, fitted))
    elif method == "cagr":
        first, last = values[0], values[-1]
        if first <= 0 or last <= 0:
            raise ProjectionError("cagr needs a first and a last complete month above zero; use linear or last_n_avg.")
        growth = (last / first) ** (1 / (count - 1)) - 1
        fitted = [first * (1 + growth) ** index for index in range(count)]
        raw = [last * (1 + growth) ** step for step in range(1, horizon + 1)]
        result = Fit({"monthly_growth": growth, "annual_growth": (1 + growth) ** 12 - 1}, fitted, [], _r2(values, fitted), _mape(values, fitted))
    else:
        window = values[-max(1, last_n) :]
        average = sum(window) / len(window)
        fitted = [average] * len(window)
        raw = [average] * horizon
        result = Fit({"average": average, "months": float(len(window))}, fitted, [], None, _mape(window, fitted))
    result.projected = [max(value, 0.0) for value in raw]
    if any(value < 0 for value in raw):
        result.clamped = True
        result.notes.append("The trend falls below zero within the horizon: those months are projected at zero.")
    return result
