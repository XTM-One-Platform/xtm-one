# SPDX-License-Identifier: Apache-2.0

"""CTEM loop measures (CTEM-30, #937): how fast the loop runs, read from the case history.

Pure functions over what is stored: a case's ``ctem_case_events`` rows and
the ``observed_at`` of the signals that opened it. No model call, nothing
estimated: a measure with no sample answers ``None`` with a count of 0, never
0 hours. Five measures, each from its own events so they reconcile with the
case history shown on the case:

- ``signal_to_case``: from the earliest origin signal the viewer may read
  (``observed_at``) to the case's ``created`` event.
- ``case_to_validation``: from ``created`` to the first ``validation`` event.
- ``case_to_decision``: from ``created`` to the first ``state`` event that
  commits the case (``in_progress`` or ``accepted_risk``). CTEM-17 adds the
  remediation decision record; until then the commitment is the decision.
- ``case_to_passing_retest``: from ``created`` to the first ``validation``
  event whose result passes (``passed`` or ``not_exposed``).
- ``closed_on_proof``: among the resolved cases, the share whose newest
  validation reference passes (resolved on proof, not on free text).

Only the cases the viewer sees are measured (``ctem_cases_view``), so a
measure never counts a case the viewer cannot open.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from statistics import median
from typing import Any

from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_cases as rules

#: The measures, in the order the page draws them.
DURATION_MEASURES = ("signal_to_case", "case_to_validation", "case_to_decision", "case_to_passing_retest")
#: State events that commit a case: the remediation is under way (``in_progress`` in old history), or its risk accepted.
DECISION_STATES = ("remediating", "in_progress", "accepted_risk")


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    start, end = rules.as_utc(start), rules.as_utc(end)
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds() / 3600.0


def _first(events: Iterable[CtemCaseEvent], predicate: Any) -> datetime | None:
    times = [rules.as_utc(e.at) for e in events if e.at is not None and predicate(e)]
    return min(times) if times else None


def case_durations(
    case: CtemCase, events: list[CtemCaseEvent], signal_times: Iterable[datetime | None]
) -> dict[str, float | None]:
    """The four durations of one case, in hours; ``None`` where the step has not happened."""
    created = _first(events, lambda e: e.kind == "created") or rules.as_utc(case.created_at)
    observed = [rules.as_utc(t) for t in signal_times if t is not None]
    return {
        "signal_to_case": _hours(min(observed), created) if observed else None,
        "case_to_validation": _hours(created, _first(events, lambda e: e.kind == "validation")),
        "case_to_decision": _hours(
            created, _first(events, lambda e: e.kind == "state" and e.to_value in DECISION_STATES)
        ),
        "case_to_passing_retest": _hours(
            created,
            _first(events, lambda e: e.kind == "validation" and e.to_value in rules.PASSING_RESULTS),
        ),
    }


def resolved_on_proof(case: CtemCase) -> bool | None:
    """``None`` for a case not resolved; else whether its newest validation reference passes."""
    if case.state != "resolved":
        return None
    return rules.passing(rules.latest_validation(case.validations))


def summarize(
    cases: list[CtemCase],
    events: Mapping[Any, list[CtemCaseEvent]],
    signal_times: Mapping[Any, list[datetime | None]],
) -> dict[str, Any]:
    """The measures over *cases* (already filtered to what the viewer sees).

    Each duration: ``{"median_hours", "count"}``; ``closed_on_proof``:
    ``{"share", "proven", "resolved"}``. No sample: ``None`` with a count of 0.
    """
    samples: dict[str, list[float]] = {m: [] for m in DURATION_MEASURES}
    proven = resolved = 0
    for case in cases:
        for name, value in case_durations(case, events.get(case.id, []), signal_times.get(case.id, [])).items():
            if value is not None:
                samples[name].append(value)
        proof = resolved_on_proof(case)
        if proof is not None:
            resolved += 1
            proven += int(proof)
    out: dict[str, Any] = {
        name: {"median_hours": round(median(values), 2) if values else None, "count": len(values)}
        for name, values in samples.items()
    }
    out["closed_on_proof"] = {
        "share": round(proven / resolved, 4) if resolved else None,
        "proven": proven,
        "resolved": resolved,
    }
    out["case_count"] = len(cases)
    return out
