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

"""AI Metrics Collector: scheduled aggregation of the platform's agentic AI usage.

Aggregates the platform's own usage (user messages, API key requests, model
usage) into ``AiAdoptionMetric`` rows for the AI Adoption dashboard. The
development tools (GitHub Copilot, Cursor) are no longer collected (#11380).

Called hourly by the autonomous scheduler.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Date as SADate
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.ai_adoption_metric import AiAdoptionMetric
from app.models.user import User

logger = logging.getLogger(__name__)

SYNC_LOOKBACK_DAYS = 30


async def _rollback_session(
    db: AsyncSession, *, context: str, force: bool = False
) -> bool:
    """Best-effort rollback after a handled error, only when actually needed.

    The catch-and-continue blocks in this module also trap non-DB failures
    (HTTP, JSON parsing) that leave the transaction perfectly healthy -
    rolling back then would discard unrelated work still pending in the
    shared session. ``Session.is_active`` is False exactly when a prior DB
    error left the transaction in a must-rollback state, so the rollback is
    gated on it rather than on the exception type (which can wrap or mask
    the underlying DB error).

    ``force=True`` skips the gate and always rolls back - used at the
    orchestration boundary in :func:`collect_all_metrics`, where the only
    pending work belongs to the collector that just FAILED (each collector
    commits on success), and none of it may leak into a later commit.

    Returns True when the pending work must be treated as LOST: the
    rollback was needed and attempted - even if the rollback call itself
    failed (fully dead connection), the invalidated transaction can never
    commit that work, so callers must reset any local counters tracking
    it. Returns False only when the transaction was healthy and pending
    work survives.
    """
    try:
        if not force and db.is_active:
            return False
        await db.rollback()
    except Exception as rollback_err:
        logger.warning(
            "Failed to rollback AI metrics DB session after %s: %s",
            context,
            format_exception_message(rollback_err),
            exc_info=True,
        )
    return True


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------


async def _upsert_metric(
    db: AsyncSession,
    *,
    metric_date: date,
    provider: str,
    user_id: uuid.UUID | None,
    user_email: str | None,
    metric_type: str,
    data: dict,
) -> None:
    """Insert or update an AI adoption metric row."""
    now = datetime.now(UTC)

    if user_id is not None:
        # user_id is set — use the unique constraint for upsert
        stmt = pg_insert(AiAdoptionMetric).values(
            id=uuid.uuid4(),
            date=metric_date,
            provider=provider,
            user_id=user_id,
            user_email=user_email,
            metric_type=metric_type,
            data=data,
            created_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ai_adoption_metric_date_provider_user_type",
            set_={"data": stmt.excluded.data, "created_at": stmt.excluded.created_at},
        )
        await db.execute(stmt)
    else:
        # user_id is NULL — unique constraint doesn't work for NULLs.
        # Manual check-then-update/insert keyed on (date, provider, user_email, metric_type).
        from sqlalchemy import update as sa_update

        existing = await db.execute(
            select(AiAdoptionMetric.id)
            .where(
                AiAdoptionMetric.date == metric_date,
                AiAdoptionMetric.provider == provider,
                AiAdoptionMetric.user_id.is_(None),
                AiAdoptionMetric.user_email == user_email,
                AiAdoptionMetric.metric_type == metric_type,
            )
            .limit(1)
        )
        row_id = existing.scalar_one_or_none()
        if row_id:
            await db.execute(
                sa_update(AiAdoptionMetric)
                .where(AiAdoptionMetric.id == row_id)
                .values(data=data, created_at=now)
            )
        else:
            await db.execute(
                pg_insert(AiAdoptionMetric).values(
                    id=uuid.uuid4(),
                    date=metric_date,
                    provider=provider,
                    user_id=None,
                    user_email=user_email,
                    metric_type=metric_type,
                    data=data,
                    created_at=now,
                )
            )


# ---------------------------------------------------------------------------
# Platform metrics collection (internal)
# ---------------------------------------------------------------------------


async def collect_platform_metrics(db: AsyncSession) -> int:
    """Aggregate internal Copilot platform usage into AiAdoptionMetric rows.

    Only counts genuine user-initiated activity (user-sent messages) per day.
    Agent-created conversations, automated assignment runs, and assistant
    messages are NOT counted — they inflate DAU without real user engagement.
    """
    from app.models.api_key import UserApiKey
    from app.models.chat import Conversation, Message

    today = date.today()
    start = today - timedelta(days=SYNC_LOOKBACK_DAYS)
    count = 0

    # Batch query: count user-sent messages per (user_id, date) in the window
    user_msg_q = await db.execute(
        select(
            Conversation.user_id,
            cast(Message.created_at, SADate).label("msg_date"),
            func.count(Message.id).label("msg_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id.isnot(None),
            Message.role == "user",
            cast(Message.created_at, SADate) >= start,
        )
        .group_by(Conversation.user_id, "msg_date")
    )

    for uid, msg_date, msg_count in user_msg_q:
        await _upsert_metric(
            db,
            metric_date=msg_date,
            provider="copilot_platform",
            user_id=uid,
            user_email=None,
            metric_type="usage",
            data={
                "source": "platform_daily",
                "messages": msg_count,
            },
        )
        count += 1

    # API key requests (cumulative, stored once per user on today)
    users_result = await db.execute(select(User).where(User.is_active.is_(True)))
    for u in users_result.scalars().all():
        key_q = await db.execute(
            select(func.coalesce(func.sum(UserApiKey.request_count), 0)).where(
                UserApiKey.user_id == u.id
            )
        )
        api_requests = key_q.scalar() or 0
        if api_requests:
            await _upsert_metric(
                db,
                metric_date=today,
                provider="copilot_platform",
                user_id=u.id,
                user_email=None,
                metric_type="cost",
                data={
                    "source": "api_key_usage",
                    "api_key_requests": api_requests,
                },
            )

    # Collect model usage from LLM proxy Redis counters
    try:
        from app.api.v1.llm_proxy import get_and_reset_model_usage

        model_usage = await get_and_reset_model_usage()
        if model_usage:
            await _upsert_metric(
                db,
                metric_date=today,
                provider="copilot_platform",
                user_id=None,
                user_email=None,
                metric_type="usage",
                data={
                    "source": "model_usage",
                    "models": model_usage,
                },
            )
            count += 1
            logger.info("Platform model usage collected: %s", model_usage)
    except Exception as e:
        logger.error("Platform model usage collection error: %s", format_exception_message(e), exc_info=True)
        if await _rollback_session(db, context="Platform model usage collection error"):
            count = 0

    return count


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def collect_all_metrics() -> dict:
    """Collect the platform's AI adoption metrics. Called by the autonomous scheduler."""
    summary: dict[str, int | str] = {}
    async with async_session() as db:
        try:
            summary["copilot_platform"] = await collect_platform_metrics(db)
            await db.commit()
        except Exception as e:
            logger.error("Platform collection failed: %s", format_exception_message(e), exc_info=True)
            summary["copilot_platform"] = f"error: {format_exception_message(e)}"
            await _rollback_session(db, context="Platform collection failure", force=True)

    logger.info("AI metrics collection complete: %s", summary)
    return summary
