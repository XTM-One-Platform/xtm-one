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

"""What a rollout costs, counted per build.

Two counters next to the queue gauges ``/metrics`` already reads at scrape
time (``xtmone_assignment_queue_depth``, ``..._oldest_age_seconds``,
``..._dispatch_lag_p95_seconds``), which they never duplicate:

- ``xtmone_tasks_orphaned_total{version}``: a running task whose execution lost
  its lease (``sub_agent.mark_task_orphaned``);
- ``xtmone_run_resumes_total{entry, cause, graceful, version}``: a resumed
  assignment run or background task, its cause a bounded class of
  ``background_checkpoint.lost_cause`` (never the free text);
- ``xtmone_chat_turn_cuts_total{visible, version}``: a chat turn cut by a
  platform stop and answered by its automatic resume, ``visible`` true when
  the person waited more than ``frictions.PERSON_VISIBLE_CUT_SECONDS`` (#11924);
- ``xtmone_chat_turn_resume_start_seconds{version}``: a histogram of the
  seconds from a chat turn's latest cut to the start of its automatic resume,
  the restart latency alone, without the resumed turn's own remaining work.

``version`` is the build that counts: the short git SHA, else the app version.
Counters are per process, like every collector of ``core/metrics.py``. The
increments never raise: a telemetry failure must not reach a resume.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.metrics import get_or_create_counter, get_or_create_histogram

logger = logging.getLogger(__name__)

TASKS_ORPHANED = get_or_create_counter(
    "xtmone_tasks_orphaned_total",
    "Running tasks whose execution lost its lease (orphaned), by build",
    ["version"],
)
RUN_RESUMES = get_or_create_counter(
    "xtmone_run_resumes_total",
    "Resumed assignment runs and background tasks, by entry, cause class, graceful and build",
    ["entry", "cause", "graceful", "version"],
)

CHAT_TURN_CUTS = get_or_create_counter(
    "xtmone_chat_turn_cuts_total",
    "Chat turns cut by a platform stop and answered on their automatic resume, by person visibility and build",
    ["visible", "version"],
)

CHAT_TURN_RESUME_START_SECONDS = get_or_create_histogram(
    "xtmone_chat_turn_resume_start_seconds",
    "Seconds from a chat turn's latest cut by a platform stop to the start of its automatic resume, by build",
    ["version"],
    buckets=(5, 10, 20, 30, 60, 90, 120, 180, 300, 600, 1800),
)


def build_version() -> str:
    """The build label: the first 12 characters of the git SHA, else the app version."""
    sha = (getattr(settings, "git_sha", "") or "").strip()
    if sha:
        return sha[:12]
    return (getattr(settings, "app_version", "") or "unknown").strip()[:32] or "unknown"


def count_orphaned() -> None:
    """One more task orphaned by this build."""
    try:
        TASKS_ORPHANED.labels(version=build_version()).inc()
    except Exception:
        logger.debug("Orphan counter not incremented", exc_info=True)


def count_resume(entry: str, cause_class: str, *, graceful: bool) -> None:
    """One more resume of *entry* (``assignment_run`` or ``background_task``) for *cause_class*."""
    try:
        RUN_RESUMES.labels(
            entry=entry, cause=cause_class, graceful="true" if graceful else "false", version=build_version()
        ).inc()
    except Exception:
        logger.debug("Resume counter not incremented", exc_info=True)


def count_chat_turn_cut(*, visible: bool) -> None:
    """One more chat turn cut and resumed by this build, person-visible or not."""
    try:
        CHAT_TURN_CUTS.labels(visible="true" if visible else "false", version=build_version()).inc()
    except Exception:
        logger.debug("Chat cut counter not incremented", exc_info=True)


def observe_chat_turn_resume_start(seconds: float | None) -> None:
    """How long a cut chat turn waited for its automatic resume to start; ``None`` is not observed."""
    if seconds is None or seconds < 0:
        return
    try:
        CHAT_TURN_RESUME_START_SECONDS.labels(version=build_version()).observe(seconds)
    except Exception:
        logger.debug("Chat resume start latency not observed", exc_info=True)
