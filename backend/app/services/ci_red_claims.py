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

"""One diagnosis run per red head commit of a pull request, per agent (#11809).

Two paths can start a run of the same agent for one red head commit:

* an event run: an ``integration_event`` or ``webhook`` assignment whose
  ``trigger_config.dedup`` key names the head
  (``{{payload.repository.full_name}}@{{payload.workflow_run.head_sha}}``,
  rendered lower case: ``owner/name@<40 hex>``);
* the CI-completion dispatch of a work item naming the pull request
  (``work_item_pull_requests._dispatch``, ``trigger_context.reason``
  ``pr_ci_completed`` with ``conclusion`` ``failure``).

The branch lease (``shared_state_pull_requests.live_pull_request_lease``)
only holds once the first run takes it, minutes after it started; in between
the second path started a second diagnosis of the same red. This module
answers, from the run table alone, whether a LIVE run of the agent already
carries the red of ``(repository, head sha)``; each path then stands down
instead of starting its run. Read failures answer ``None`` (fail open: the
run starts, as before).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: How far back a live run is looked for: the longest configured dedup window.
LOOKBACK_SECONDS = 3600
#: Live runs of one agent read per check; an agent never has that many.
_MAX_RUNS = 200
#: ``trigger_context.reason`` of a work-item CI-completion dispatch
#: (``work_item_pull_requests.CI_COMPLETED_REASON``, kept equal by a test).
_CI_COMPLETED_REASON = "pr_ci_completed"
_RED_HEAD = re.compile(r"(?:^|[^a-z0-9_.\-/])([a-z0-9_.\-]+/[a-z0-9_.\-]+)@([0-9a-f]{40})(?![0-9a-f])")


def red_head_of_key(dedup_key: object) -> tuple[str, str] | None:
    """``(owner/name, sha)`` a configured dedup key names, else ``None``. Pure.

    Only a key rendered from ``trigger_config.dedup`` (``cfg:`` prefix) counts:
    a mail or calendar key never names a head commit.
    """
    from app.services.event_run_dedup import is_configured_key

    if not is_configured_key(dedup_key):
        return None
    match = _RED_HEAD.search(str(dedup_key).lower())
    if match is None:
        return None
    return match.group(1), match.group(2)


def carries_red_head(
    trigger_context: object,
    repo: str,
    head_sha: str,
    *,
    exclude_item_id: object = None,
) -> bool:
    """Whether a run started with *trigger_context* diagnoses the red of *repo* at *head_sha*. Pure.

    An event run whose configured dedup key names the head, or a work-item
    CI-completion dispatch of a failure on that head (another item than
    *exclude_item_id*).
    """
    if not isinstance(trigger_context, Mapping) or not repo or not head_sha:
        return False
    want = (repo.strip().lower(), head_sha.strip().lower())
    from app.services.event_run_dedup import DEDUP_KEY_CONTEXT, DUPLICATE_OF_CONTEXT

    if trigger_context.get(DUPLICATE_OF_CONTEXT):
        return False  # refused as a duplicate: never ran
    if red_head_of_key(trigger_context.get(DEDUP_KEY_CONTEXT)) == want:
        return True
    if trigger_context.get("reason") != _CI_COMPLETED_REASON or trigger_context.get("conclusion") != "failure":
        return False
    if exclude_item_id is not None and str(trigger_context.get("work_item_id")) == str(exclude_item_id):
        return False
    pull = trigger_context.get("pull_request")
    if not isinstance(pull, Mapping):
        return False
    got = (str(pull.get("repo") or "").strip().lower(), str(pull.get("head_sha") or "").strip().lower())
    return got == want


async def live_red_carrier(
    db: Any,
    agent_id: object,
    repo: str,
    head_sha: str,
    *,
    exclude_item_id: object = None,
    exclude_run_id: object = None,
) -> uuid.UUID | None:
    """The live run of *agent_id* that already diagnoses the red of *repo* at *head_sha*, else ``None``.

    Live: one of ``work_item.LIVE_RUN_STATUSES``, started within
    :data:`LOOKBACK_SECONDS`. The oldest one wins. An event run keyed on the
    head counts whatever woke it, a green listener about to merge included:
    the inbox pass asks for every open head, not only a red one. Never raises:
    a failed read answers ``None`` so the caller starts its run as before.
    """
    if agent_id is None or not repo or not head_sha:
        return None
    try:
        from sqlalchemy import select

        from app.models.assignment import AssignmentRun
        from app.services.work_item import LIVE_RUN_STATUSES

        since = datetime.now(UTC) - timedelta(seconds=LOOKBACK_SECONDS)
        rows = (
            await db.execute(
                select(AssignmentRun.id, AssignmentRun.trigger_context)
                .where(
                    AssignmentRun.agent_id == agent_id,
                    AssignmentRun.status.in_(LIVE_RUN_STATUSES),
                    AssignmentRun.created_at >= since,
                )
                .order_by(AssignmentRun.created_at, AssignmentRun.id)
                .limit(_MAX_RUNS)
            )
        ).all()
    except Exception as exc:  # fail open: the run starts as before
        logger.debug("Live red carrier of %s@%s not read: %s", repo, head_sha, exc)
        return None
    for run_id, context in rows:
        if exclude_run_id is not None and str(run_id) == str(exclude_run_id):
            continue
        if carries_red_head(context, repo, head_sha, exclude_item_id=exclude_item_id):
            return run_id
    return None


def standing_down_note(run_id: object, repo: str, number: object, head_sha: str) -> str:
    """What a CI-completion dispatch that stood down for *run_id* says on the item."""
    return (
        f"not dispatched: run {run_id} already diagnoses the red of {repo}#{number} on "
        f"{str(head_sha)[:12]}; the inbox takes it once that run ends"
    )
