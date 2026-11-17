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

"""Tell the owners once that a bound integration turned invalid (#9898).

A bound integration whose stored extra_config lacks a key its provider needs
(catalog ``extra_field_required_when``) yields no credential, so the tool build
drops every one of its tools. The turn already says so to the model
(:func:`app.services.integration_credentials.format_invalid_integration_prompt_note`);
this module is the one loud signal to the people who can fix it: the first turn
that meets the integration in that state sends one notification-bell entry to
the integration's owner and to the owner of the agent whose turn met it, naming
the missing keys and linking to the integration's page.

"Once" is a Redis ``SET NX`` on a key per integration and per set of missing
keys, kept a week: a new missing key is a new state and is told again, the same
state is not repeated every turn. A Redis that cannot answer sends nothing, so
a broken integration never becomes a notification storm. Delivery uses
:func:`app.services.notify.notify_web_user` in a session of its own, so the
caller's transaction is never touched, and never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterable
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

NOTICE_KEY_PREFIX = "integration:invalid_notified:"
#: A week: the same invalid state is told again at most once a week.
NOTICE_TTL_SECONDS = 7 * 24 * 3600
NOTIFICATION_SOURCE = "integration_invalid"
SOURCE_LABEL = "Integrations"
_REDIS_TIMEOUT_SECONDS = 0.5


def notice_key(integration_id: Any, missing: Iterable[str]) -> str:
    """The Redis key marking *integration_id*'s notice for this set of missing keys as sent."""
    digest = hashlib.sha256(",".join(sorted({str(k) for k in missing})).encode()).hexdigest()[:16]
    return f"{NOTICE_KEY_PREFIX}{integration_id}:{digest}"


def recipients(integration_owner_id: Any, agent_owner_id: Any) -> list[str]:
    """The integration's owner first, then the agent's owner, each once."""
    seen: list[str] = []
    for user_id in (integration_owner_id, agent_owner_id):
        if user_id and str(user_id) not in seen:
            seen.append(str(user_id))
    return seen


async def _first_time(key: str) -> bool:
    """Whether this call takes the notice; ``False`` when Redis cannot answer."""
    try:
        from app.core.redis import redis_client

        redis = await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
        taken = await asyncio.wait_for(
            redis.set(key, "1", nx=True, ex=NOTICE_TTL_SECONDS),
            _REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Invalid integration notice not taken (%s): %s", key, format_exception_message(exc))
        return False
    return bool(taken)


async def _agent_owner_id(db: Any, agent_id: Any) -> str | None:
    if not agent_id:
        return None
    import uuid

    from sqlalchemy import select

    from app.models.agent import Agent

    try:
        parsed = uuid.UUID(str(agent_id))
    except (ValueError, TypeError):
        return None
    owner = (await db.execute(select(Agent.user_id).where(Agent.id == parsed))).scalar_one_or_none()
    return str(owner) if owner else None


async def notify_integration_invalid_once(
    integration: Any,
    missing: list[str],
    *,
    agent_id: Any = None,
    agent_name: str | None = None,
) -> bool:
    """Send the notice for *integration* in this invalid state if not sent yet; whether it was sent.

    Never raises.
    """
    integration_id = getattr(integration, "id", None)
    if integration_id is None or not missing:
        return False
    if not await _first_time(notice_key(integration_id, missing)):
        return False
    try:
        from app.core.database import async_session
        from app.services.notify import notify_web_user

        name = getattr(integration, "name", None) or str(integration_id)
        provider = getattr(integration, "provider", None) or "integration"
        keys = ", ".join(f"'{key}'" for key in sorted(set(missing)))
        title = f"Integration '{name}' is invalid: {keys} missing, its tools are unavailable"
        met = f" Agent '{agent_name}' met it on a turn." if agent_name else ""
        summary = (
            f"The {provider} integration '{name}' lacks {keys} in its configuration for its current "
            f"settings, so it yields no credential and none of its tools reach any agent.{met} "
            "Set the key on the integration's page. This notice is sent once per invalid state."
        )
        async with async_session() as db:
            to = recipients(getattr(integration, "user_id", None), await _agent_owner_id(db, agent_id))
            for user_id in to:
                await notify_web_user(
                    db,
                    user_id,
                    title=title,
                    summary=summary,
                    source_label=SOURCE_LABEL,
                    extra_data={
                        "notification_source": NOTIFICATION_SOURCE,
                        "integration_id": str(integration_id),
                        "missing_keys": sorted(set(missing)),
                        "agent_id": str(agent_id) if agent_id else None,
                        "path": f"/integrations/{integration_id}",
                    },
                )
            await db.commit()
        logger.warning(
            "Integration %s (%s) is invalid (%s missing); notified %d user(s).",
            name,
            integration_id,
            keys,
            len(to),
        )
        return True
    except Exception as exc:
        logger.warning(
            "Invalid integration notice for %s could not be sent: %s",
            integration_id,
            format_exception_message(exc),
        )
        return False
