"""Where a recorded take's frames show the page of a scroll, as ``scroll_pacing`` draws them.

A real-browser test judging that a scroll plays without a held frame compares each frame with the one before only
where the page moved between them: never on the scroll log's clock alone, which runs ahead of the paint's - a
scroll the wheel turned is drawn as late as the page painted it (``late``), so the log's first moving instants show
the page still at rest.

The record is rounded (``late`` to the millisecond, the wheel's instants to a tenth of one): a frame the pacer drew a
fraction of a millisecond before the wheel's first step reads, on the rounded record, as one after it. A move
is therefore the least any lag the record's precision allows gives, never the rounded value's alone.
"""

from __future__ import annotations

import numpy as np

#: How far (s) the pacer's own lag may stand from the record's: ``late`` rounded to 3 decimals, each wheel instant to 4.
LATE_PRECISION = 0.0005 + 0.00005


def _wheel(scroll: dict) -> tuple[list[float], list[float]] | None:
    """The wheel's instants and shares (0 to 1) of a paced scroll the wheel turned, else None (drawn on its log)."""
    wheel = scroll.get("wheel")
    if not (scroll.get("paced") and wheel and wheel[-1][1] > 0):
        return None
    turned_at, turned = zip(*wheel, strict=True)
    return [float(t) for t in turned_at], [value / turned[-1] for value in turned]


def shown_at(scroll: dict, t: float, *, late: float | None = None) -> float:
    """Where the take's frame at *t* shows the page (CSS px) for one of its scrolls (``scroll_pacing.pace``'s record):
    a paced scroll the wheel turned on the wheel's clock, as late as the page painted it (``wheel``, ``late``, or the
    *late* given), else on its log."""
    log_t, log_top = zip(*scroll["log"], strict=True)
    wheel = _wheel(scroll)
    if wheel is not None:
        lag = float(scroll.get("late") or 0.0) if late is None else late
        share = float(np.interp(t - lag, *wheel))
        return log_top[0] + (log_top[-1] - log_top[0]) * share
    return float(np.interp(t, log_t, log_top))


def moved(scroll: dict, earlier: float, later: float) -> float:
    """How far (CSS px) the page moved between the frames at *earlier* and *later* (:func:`shown_at`): the least over
    the lags the record's rounding allows (:data:`LATE_PRECISION`). The move is piecewise linear in the lag, so the
    least stands at an end of that window or where either frame meets one of the wheel's instants."""
    wheel = _wheel(scroll)
    if wheel is None:
        return abs(shown_at(scroll, later) - shown_at(scroll, earlier))
    recorded = float(scroll.get("late") or 0.0)
    low, high = recorded - LATE_PRECISION, recorded + LATE_PRECISION
    lags = {low, recorded, high}
    for instant in wheel[0]:
        for t in (earlier, later):
            if low < t - instant < high:
                # Either side of the step: np.interp at a repeated instant reads the share after it.
                lags.update({t - instant - 1e-9, t - instant + 1e-9})
    return min(abs(shown_at(scroll, later, late=lag) - shown_at(scroll, earlier, late=lag)) for lag in lags)
