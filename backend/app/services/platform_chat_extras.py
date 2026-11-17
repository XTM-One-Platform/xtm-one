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

"""What the chat embedded in OpenCTI / OpenAEV shows beside the transcript.

The web chat's composer carries a quota indicator and a prompt picker, and its
answers can be rated. The embedded chat (``@filigran/chatbot``) reaches XTM One
through the platform router only, so ``api/v1/platform.py`` serves the same
three things from here, with the web chat's own semantics:

* :func:`resolve_platform_chat_quota` - the numbers ``QuotaIndicator`` shows,
  derived from the payload ``GET /settings/my-quotas`` serves, so the two
  surfaces can never show different numbers;
* :func:`list_platform_chat_prompts` - the web prompt picker's list: the
  three-tier library set, ``hide_from_chat`` excluded, no admin bypass;
* :func:`load_user_feedback_by_message` - the caller's ratings over a restored
  window, in one query. Writing a rating reuses the web chat's own path
  (``chat.apply_message_feedback`` / ``chat.retract_message_feedback``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import get_user_group_ids, user_visible_query
from app.api.v1.schemas.settings import QuotaConfig, QuotaUsageEntry
from app.core.redis import redis_client
from app.models.chat import MessageFeedback
from app.models.prompt import Prompt
from app.models.user import User
from app.services.quota import (
    get_agentic_quota_periods,
    get_user_quota_usage,
    resolve_full_quota_info,
)

# The period of the legacy single-period fields when no agentic quota resolves:
# the bucket the enforced counters fall back to (``increment_agentic_usage``).
# Kept for chatbot versions that read ``used`` / ``limit`` / ``period`` only;
# the current one reads ``periods`` and shows today by default.
DEFAULT_QUOTA_PERIOD = "monthly"

# The web picker lists every visible prompt; the embedded one is a menu in a
# side panel, and a response is never unbounded.
PLATFORM_CHAT_PROMPT_LIMIT = 200


def merge_sandbox_into_agentic_count(
    usage: Sequence[Mapping[str, Any]],
    agentic_entry: Mapping[str, Any] | None,
) -> int:
    """The agentic count a quota indicator shows: agentic plus the sandbox slice.

    The backend twin of ``mergeSandboxIntoAgenticCount``
    (``frontend/src/lib/agentic-quota.ts``); both are checked against
    ``frontend/tests_e2e/fixtures/agentic-quota-count-cases.json``. Sandbox usage
    has no quota of its own and is reported under agentic's period, so the two
    counts add up - except for a ``global`` agentic entry, the shared pool,
    which already counts global agentic + global sandbox and is returned
    untouched.
    """
    agentic_count = int((agentic_entry or {}).get("count") or 0)
    if agentic_entry is not None and agentic_entry.get("scope") == "global":
        return agentic_count
    sandbox = next(
        (u for u in usage if u.get("usage_type") == "sandbox" and u.get("model") is None),
        None,
    )
    return agentic_count + int((sandbox or {}).get("count") or 0)


def agentic_total_entry(usage: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The current-period agentic total: the entry ``QuotaIndicator`` reads."""
    return next(
        (u for u in usage if u.get("usage_type") == "agentic" and u.get("model") is None),
        None,
    )


async def resolve_platform_chat_quota(db: AsyncSession, user: User) -> dict[str, Any] | None:
    """The agentic quota the embedded chat shows, or ``None`` without a licence.

    The same reading as the web chat's ``QuotaIndicator`` over
    ``GET /settings/my-quotas``: the resolved agentic ``default`` limit
    (``None`` = unlimited) and ``period``, the agentic total merged with the
    sandbox slice, and the scope of that total (``global`` for the shared
    pool), plus ``periods``: today, this month and this year, the rows of the
    quota panel (:func:`get_agentic_quota_periods`, the same rows
    ``/my-quotas`` serves). The resolved config and the usage go through the response models
    ``/my-quotas`` serves them with, so a default either one fills in is filled
    in here too. ``None`` wherever agentic AI is not licensed (Community
    Edition), where the web chat renders no indicator.
    """
    from app.api.v1.platform import is_agentic_ai_licensed

    if not await is_agentic_ai_licensed(db):
        return None

    redis = await redis_client()
    resolved, _sources = await resolve_full_quota_info(db, redis, user.id)
    raw_usage = await get_user_quota_usage(db, user.id)

    agentic = QuotaConfig(**resolved).agentic if resolved else None
    usage = [QuotaUsageEntry(**entry).model_dump() for entry in raw_usage]
    total = agentic_total_entry(usage)
    return {
        "used": merge_sandbox_into_agentic_count(usage, total),
        "limit": agentic.default if agentic is not None else None,
        "period": agentic.period if agentic is not None else DEFAULT_QUOTA_PERIOD,
        "scope": total["scope"] if total is not None else "user",
        "periods": await get_agentic_quota_periods(db, user.id),
    }


async def list_platform_chat_prompts(db: AsyncSession, user: User) -> list[Prompt]:
    """The prompts the web chat's picker offers, in its order.

    Own + shared + company-managed (``user_visible_query``, the library set:
    no admin bypass), without the ``hide_from_chat`` ones (a NULL flag
    shows, as ``!p.hide_from_chat`` does), newest first, bounded by
    :data:`PLATFORM_CHAT_PROMPT_LIMIT`.
    """
    group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(Prompt, user.id, group_ids)
        .where(Prompt.hide_from_chat.is_not(True))
        .order_by(Prompt.id)
        .limit(PLATFORM_CHAT_PROMPT_LIMIT)
    )
    return list(result.scalars().all())


async def load_user_feedback_by_message(
    db: AsyncSession,
    message_ids: Collection[uuid.UUID],
    user_id: uuid.UUID,
) -> dict[uuid.UUID, MessageFeedback]:
    """The caller's own rating of each of *message_ids* that has one - one query.

    Keyed by message id. Another user's rating of the same message is never
    read: a rating is one user's, like ``chat.load_message_feedback``.
    """
    if not message_ids:
        return {}
    result = await db.execute(
        select(MessageFeedback).where(
            MessageFeedback.message_id.in_(list(message_ids)),
            MessageFeedback.user_id == user_id,
        )
    )
    return {fb.message_id: fb for fb in result.scalars().all()}
