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

"""What a run's end says about its agent, and a member's health verdict.

One rule, read by every surface that judges an agent from its runs: the fleet
Members panel badge (``GET /fleets/{id}/health``), the fleet activity feed
(``GET /fleets/{id}/activity``) and the run list (``AssignmentRunDetailResponse
.outcome``). A surface never re-spells it.

* :func:`run_outcome` classifies one run: ``ok`` (completed), ``failed`` (the
  agent's own work failed), ``interrupted`` (the platform ended it: a rollout
  cut, the stale-run cleanup after a restart, a consult cancelled with its
  caller, a run abandoned with its worker), ``active`` (not ended) or ``other``
  (cancelled, skipped, dismissed...). An interrupted run keeps its ``failed``
  status and stays listed: only the verdict leaves it out.
* :func:`member_verdict` judges a member over its recent runs: it is failing
  when the latest of its recent ended runs that was not interrupted failed. A
  failure followed by a success clears it; interruptions alone never make it
  fail. The counts it returns are what the badge's tooltip says.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: ``run_outcome`` values.
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_ACTIVE = "active"
OUTCOME_OTHER = "other"

#: How far back a run counts in a member's verdict.
HEALTH_WINDOW = timedelta(hours=24)
#: How many of a member's latest runs the verdict reads.
HEALTH_RECENT_RUNS = 10

#: Words of the errors the platform writes when IT ends a run, not the agent.
#: Each is pinned against the constant that writes it by
#: ``tests/test_run_outcome.py``: a reworded constant fails the test.
INTERRUPTION_MARKERS: tuple[str, ...] = (
    # consult_watchdog.CONSULT_ROLLOUT_ERROR and ConsultCutError.
    "cut by a rollout",
    # consult_watchdog.CONSULT_CANCELLED_ERROR: the caller went first.
    "Consult cancelled before the consulted agent answered",
    # autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX: shutdown, pod restart.
    "Run marked as failed by stale-assignment-run cleanup.",
    # orchestration_adapters.assignment.WORKER_GONE_DEAD_LETTER_ERROR.
    "its worker was lost",
)


def is_interruption(error: str | None) -> bool:
    """Whether a run's error is one the platform wrote when it ended the run."""
    if not error:
        return False
    return any(marker in error for marker in INTERRUPTION_MARKERS)


def run_outcome(status: str | None, error: str | None) -> str:
    """What one run's end says about its agent (see the module docstring)."""
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES

    if status == "completed":
        return OUTCOME_OK
    if status == "failed":
        return OUTCOME_INTERRUPTED if is_interruption(error) else OUTCOME_FAILED
    if status in ACTIVE_RUN_STATUSES or status == "awaiting_human":
        return OUTCOME_ACTIVE
    return OUTCOME_OTHER


@dataclass(frozen=True)
class RunFact:
    """One run as the verdict reads it."""

    created_at: datetime
    status: str
    error: str | None = None


@dataclass(frozen=True)
class MemberVerdict:
    failing: bool
    #: Ended runs of the window that were not interrupted (the tooltip's "of the last N").
    recent_runs: int
    #: The failed ones among them.
    recent_failures: int
    #: Interrupted runs of the window.
    recent_interruptions: int
    #: The member's latest run, whatever it is (the row's "Last run").
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_outcome: str | None


def member_verdict(runs: Sequence[RunFact], *, now: datetime | None = None) -> MemberVerdict:
    """A member's verdict from its latest runs, newest first.

    The window is the :data:`HEALTH_RECENT_RUNS` latest runs created in the
    last :data:`HEALTH_WINDOW`. Failing: the newest ended run of the window
    that was not interrupted failed.
    """
    now = now or datetime.now(UTC)
    latest = runs[0] if runs else None
    window = [r for r in runs[:HEALTH_RECENT_RUNS] if r.created_at >= now - HEALTH_WINDOW]
    outcomes = [run_outcome(r.status, r.error) for r in window]
    judged = [o for o in outcomes if o in (OUTCOME_OK, OUTCOME_FAILED)]
    return MemberVerdict(
        failing=bool(judged) and judged[0] == OUTCOME_FAILED,
        recent_runs=len(judged),
        recent_failures=judged.count(OUTCOME_FAILED),
        recent_interruptions=outcomes.count(OUTCOME_INTERRUPTED),
        last_run_at=latest.created_at if latest else None,
        last_run_status=latest.status if latest else None,
        last_run_outcome=run_outcome(latest.status, latest.error) if latest else None,
    )
