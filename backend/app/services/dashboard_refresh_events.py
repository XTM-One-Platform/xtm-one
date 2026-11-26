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

"""Tell the open pages of a dashboard that a data source has a new snapshot (``dashboard_refresh``).

One frame on the activity stream (``api/v1/activity.py``), ids only -
``{"type": "dashboard.source_updated", "dashboard_id", "source_id"}`` - so a
page refetches that source through the API, which applies the reader's
access on every read. Routed to those who may open the dashboard: an
unrestricted company dashboard on ``activity:company``; otherwise its owner,
the people it is shared with and the members of the groups it is shared
with, each on their own channel. Best effort, after the commit: a lost frame
costs a page only its own auto-refresh, the floor.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from sqlalchemy import select

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: After a publish failure, skip publishing for this long (seconds).
_FAILURE_COOLDOWN_SECONDS = 30.0
_down_until: float = 0.0


async def _channels(dashboard_id: str) -> list[str]:
    from app.core.database import async_session
    from app.models.custom_dashboard import CustomDashboard
    from app.models.group import GroupMember

    async with async_session() as db:
        dashboard = await db.get(CustomDashboard, uuid.UUID(dashboard_id))
        if dashboard is None:
            return []
        if dashboard.is_company_managed and dashboard.access_restricted is not True:
            return ["activity:company"]
        readers = {str(dashboard.user_id)} if dashboard.user_id else set()
        readers |= {str(u) for u in dashboard.shared_user_ids or []}
        groups = [uuid.UUID(str(g)) for g in dashboard.shared_group_ids or []]
        if groups:
            members = await db.execute(select(GroupMember.user_id).where(GroupMember.group_id.in_(groups)))
            readers |= {str(u) for u in members.scalars()}
    return [f"activity:{reader}" for reader in sorted(readers)]


async def publish_source_updated(dashboard_id: str, source_id: str) -> None:
    """Publish one ``dashboard.source_updated`` frame to the dashboard's readers; never raises."""
    global _down_until
    if time.monotonic() < _down_until:
        return
    try:
        from app.core.redis import redis_client

        channels = await _channels(dashboard_id)
        if not channels:
            return
        redis = await redis_client()
        payload = json.dumps({"type": "dashboard.source_updated", "dashboard_id": dashboard_id, "source_id": source_id})
        for channel in channels:
            await redis.publish(channel, payload)
    except Exception as exc:
        _down_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "dashboard source SSE publish failed (quiet for %.0fs): %s", _FAILURE_COOLDOWN_SECONDS, format_exception_message(exc)
        )


__all__ = ["publish_source_updated"]
