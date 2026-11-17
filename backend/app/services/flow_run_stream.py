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

"""Live flow-run updates on the SSE activity stream.

The walker commits the step journal as it goes (one commit per node
transition — see ``services/flow_runner.py``), and this module tells the
browser *when to look*: every transition publishes a small
``flow_run_update`` frame on the same Redis channels the assignment engine
uses, and the frontend reacts by refetching the run detail it already knows
how to render.  The frames deliberately carry only identifiers and statuses —
never step inputs/outputs — because the SSE channel is per-user/company while
run *content* visibility is enforced by the REST endpoints.

Publishing is best-effort: a dead Redis must never slow a run down, so after
a failure the publisher goes quiet for a cooldown instead of paying the
connect timeout on every node.
"""

from __future__ import annotations

import json
import logging
import time

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.flow_run import FlowRun

logger = logging.getLogger(__name__)

# After a publish failure, skip publishing for this long (seconds) — polling
# is the floor, so losing the accelerator briefly costs nothing.
_FAILURE_COOLDOWN_SECONDS = 30.0
_down_until: float = 0.0


async def publish_flow_run_update(
    run: FlowRun,
    *,
    node_id: str | None = None,
    node_status: str | None = None,
) -> None:
    """Publish one run/step transition to the SSE activity stream.

    Call AFTER the transition is committed — the frame triggers a refetch,
    so an uncommitted transition would refetch into stale data.

    Channel routing follows ``get_visible_run``'s tiers: unrestricted
    company-managed runs broadcast on ``activity:company``; everything else
    (personal, ``access_restricted`` company, shared) goes to the owner's
    ``activity:{user_id}`` only — other viewers fall back to polling, and
    the journal endpoints enforce visibility on every refetch anyway.
    """
    global _down_until
    if time.monotonic() < _down_until:
        return
    try:
        redis = await redis_client()
        payload = json.dumps(
            {
                "type": "flow_run_update",
                "run_id": str(run.id),
                "flow_id": str(run.flow_id),
                "status": run.status,
                "node_id": node_id,
                "node_status": node_status,
            }
        )
        # Mirror `get_visible_run` tier for tier: company-managed is a
        # blanket grant only while the flow is NOT narrowed to explicit
        # grants. A restricted or owner-less run has no safe
        # broadcast channel, so it falls back to polling — the documented
        # floor — rather than telling the whole company it exists.
        if run.is_company_managed and run.access_restricted is not True:
            await redis.publish("activity:company", payload)
        elif run.user_id is not None:
            await redis.publish(f"activity:{run.user_id}", payload)
    except Exception as exc:
        _down_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "flow run SSE publish failed (quiet for %.0fs): %s",
            _FAILURE_COOLDOWN_SECONDS,
            format_exception_message(exc),
        )
