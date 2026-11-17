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

"""Tell the owner and the administrators once a day that an agent's quota is spent.

When an agent's agentic quota is exhausted, its deliveries are dropped without
a run (``assignment_engine._drop_delivery_over_quota``) and its queued runs end
``skipped`` at dequeue. Both are quiet by design: no run, no ``run.failed``.
This module is the one loud signal: the first refusal of the day (UTC) for an
agent sends one notification-bell entry to the agent's owner and to every
active platform administrator, naming the assignment that met the refusal.

The "first of the day" is a Redis ``SET NX`` on a key per agent and day, with
a TTL a little over a day. A Redis that cannot answer sends nothing: a refusal
storm must never become a notification storm. Delivery uses
:func:`app.services.notify.notify_web_user` in a session of its own, so the
caller's transaction is never touched, and never raises.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

NOTICE_KEY_PREFIX = "assignment:quota_exhausted_notified:"
#: A day and an hour: the key of a day expires after that day is over.
NOTICE_TTL_SECONDS = 25 * 3600
NOTIFICATION_SOURCE = "agentic_quota_exhausted"
SOURCE_LABEL = "Agentic quota"
_REDIS_TIMEOUT_SECONDS = 0.5


def notice_key(agent_id: Any, now: datetime | None = None) -> str:
    """The Redis key that marks *agent_id*'s notice of the UTC day of *now* as sent."""
    day = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")
    return f"{NOTICE_KEY_PREFIX}{agent_id}:{day}"


async def _first_of_the_day(agent_id: Any, now: datetime | None) -> bool:
    """Whether this call takes the agent's notice of the day; ``False`` when Redis cannot answer."""
    try:
        from app.core.redis import redis_client

        redis = await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
        taken = await asyncio.wait_for(
            redis.set(notice_key(agent_id, now), "1", nx=True, ex=NOTICE_TTL_SECONDS),
            _REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Quota exhausted notice not taken for agent %s: %s", agent_id, format_exception_message(exc))
        return False
    return bool(taken)


async def _admin_ids(db: Any) -> list[str]:
    """Every active platform administrator's id."""
    from sqlalchemy import select

    from app.models.user import User

    rows = (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
    return [str(user_id) for user_id in rows]


def _recipients(owner_id: Any, admin_ids: list[str]) -> list[str]:
    """The owner first, then the administrators, each once."""
    seen: list[str] = [str(owner_id)] if owner_id else []
    for user_id in admin_ids:
        if user_id not in seen:
            seen.append(user_id)
    return seen


async def notify_quota_exhausted_once(
    assignment: Any,
    agent: Any,
    error: str,
    *,
    where: str,
    now: datetime | None = None,
) -> bool:
    """Send the agent's quota notice of the day if this is its first refusal; whether it was sent.

    *where* says which path met the refusal (``"delivery"``: a webhook or
    integration event dropped before any run; ``"dequeue"``: a queued run
    skipped). Never raises.
    """
    agent_id = getattr(agent, "id", None)
    if agent_id is None or not await _first_of_the_day(agent_id, now):
        return False
    try:
        from app.core.database import async_session
        from app.services.notify import notify_web_user

        agent_name = getattr(agent, "name", None) or str(agent_id)
        assignment_name = getattr(assignment, "name", None) or str(getattr(assignment, "id", ""))
        what = (
            "deliveries are dropped without a run"
            if where == "delivery"
            else "queued runs are skipped before they start"
        )
        title = f"Agent '{agent_name}' has spent its agentic quota: assignment '{assignment_name}' stopped running"
        summary = (
            f"{error}. Until the quota resets, its {what}. Raise the quota in Settings, or cancel the "
            "waiting runs from Operations. This notice is sent once a day per agent."
        )
        async with async_session() as db:
            recipients = _recipients(getattr(agent, "user_id", None), await _admin_ids(db))
            for user_id in recipients:
                await notify_web_user(
                    db,
                    user_id,
                    title=title,
                    summary=summary,
                    source_label=SOURCE_LABEL,
                    extra_data={
                        "notification_source": NOTIFICATION_SOURCE,
                        "agent_id": str(agent_id),
                        "assignment_id": str(getattr(assignment, "id", "")),
                        "where": where,
                        "path": f"/agents/{agent_id}",
                    },
                )
            await db.commit()
        logger.warning(
            "Agent %s (%s) crossed its agentic quota on assignment '%s' (%s); notified %d user(s).",
            agent_name,
            agent_id,
            assignment_name,
            where,
            len(recipients),
        )
        return True
    except Exception as exc:
        logger.warning(
            "Quota exhausted notice for agent %s could not be sent: %s", agent_id, format_exception_message(exc)
        )
        return False
