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

"""The models traces ran on that have no price, and the notice that names them.

A trace of a model with no pricing rule is stored with a NULL cost, and every
cost total leaves it out: cost is under-reported with nothing to say so.
:func:`unpriced_models` lists them (the Model pricing screen, and
``query_platform_analytics`` over its window); :func:`notify_unpriced_models_if_due`
tells the administrators, in the bell, once per model, of a model the last
:data:`NOTICE_LOOKBACK_DAYS` of traces ran on without a price. A price set with
an effective date fills the cost of the traces from that date on
(``repricing.reprice_null_costs`` with ``enforce_effective_from``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.llm_model_price import LLMModelPrice
from app.models.llm_trace import LLMTrace

logger = logging.getLogger(__name__)

NOTICE_INTERVAL_SECONDS = 24 * 3600
NOTICE_LOOKBACK_DAYS = 7
LEASE_KEY = "llm_pricing:unpriced_models_notice_lease"
#: The models already announced: a model is announced once, until it is priced and seen unpriced again.
NOTIFIED_KEY = "llm_pricing:unpriced_models_notified"
NOTIFICATION_SOURCE = "unpriced_models"
SOURCE_LABEL = "Model pricing"
SETTINGS_PATH = "/settings/usage/pricing"

_running: set[asyncio.Task] = set()


async def price_rules(db: AsyncSession):
    """A predicate ``model -> bool``: whether a pricing row matches *model* (its id, or its pattern)."""
    rows = (
        await db.execute(
            select(LLMModelPrice.model_id, LLMModelPrice.model_pattern).where(
                LLMModelPrice.model_id.is_not(None), LLMModelPrice.model_id != ""
            )
        )
    ).all()
    exact = {str(row.model_id) for row in rows if row.model_id}
    patterns: list[re.Pattern[str]] = []
    for row in rows:
        if row.model_pattern:
            try:
                patterns.append(re.compile(str(row.model_pattern)))
            except re.error:
                continue

    def has_price(model: str) -> bool:
        return model in exact or any(pattern.search(model) for pattern in patterns)

    return has_price


async def null_cost_models(db: AsyncSession, traces: Any = None) -> list[dict[str, Any]]:
    """Every model with traces of no cost, most traces first: ``{model, trace_count, tokens, has_price}``.

    *traces* narrows the read (a select of ``llm_traces`` rows, as the analytics
    filters build it); all traces otherwise. ``has_price`` says a rule matches
    the model now: its traces only wait for a reprice.
    """
    base = (traces if traces is not None else select(LLMTrace)).subquery()
    rows = (
        await db.execute(
            select(
                base.c.model.label("model"),
                func.count(base.c.id).label("trace_count"),
                func.coalesce(func.sum(base.c.total_tokens), 0).label("tokens"),
            )
            .where(base.c.total_cost.is_(None), base.c.model.is_not(None), base.c.model != "")
            .group_by(base.c.model)
            .order_by(func.count(base.c.id).desc(), base.c.model.asc())
        )
    ).all()
    has_price = await price_rules(db)
    return [
        {
            "model": str(row.model),
            "trace_count": int(row.trace_count),
            "tokens": int(row.tokens or 0),
            "has_price": has_price(str(row.model)),
        }
        for row in rows
    ]


async def unpriced_models(db: AsyncSession, traces: Any = None) -> list[dict[str, Any]]:
    """The models of :func:`null_cost_models` that no pricing rule matches."""
    return [item for item in await null_cost_models(db, traces) if not item["has_price"]]


async def notify_unpriced_models_if_due() -> bool:
    """Start :func:`notify_unpriced_models` when this process takes the daily lease; whether it did."""
    try:
        redis = await redis_client()
        if not await redis.set(LEASE_KEY, "1", nx=True, ex=NOTICE_INTERVAL_SECONDS):
            return False
    except Exception as exc:
        logger.debug("Unpriced model notice lease not taken: %s", format_exception_message(exc))
        return False
    task = asyncio.create_task(_notify_logged(redis), name="unpriced-model-notice")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True


async def _notify_logged(redis: Any) -> None:
    from app.core.database import async_session

    try:
        async with async_session() as db:
            await notify_unpriced_models(db, redis)
    except Exception as exc:
        logger.error("Unpriced model notice error: %s", format_exception_message(exc), exc_info=True)


async def notify_unpriced_models(db: AsyncSession, redis: Any, *, now: datetime | None = None) -> list[str]:
    """Tell every active administrator of the models newly seen without a price; the models announced.

    A model is announced once; once priced it leaves the memory, so it is
    announced again if it ever runs unpriced again.
    """
    from app.models.user import User
    from app.services.notify import notify_web_user

    now = now or datetime.now(UTC)
    since = now - timedelta(days=NOTICE_LOOKBACK_DAYS)
    recent = select(LLMTrace).where(LLMTrace.started_at >= since, LLMTrace.started_at < now)
    unpriced = await unpriced_models(db, recent)
    current = {item["model"] for item in unpriced}
    known = {value.decode() if isinstance(value, bytes) else str(value) for value in await redis.smembers(NOTIFIED_KEY)}
    for model in known - current:
        await redis.srem(NOTIFIED_KEY, model)
    new = [item for item in unpriced if item["model"] not in known]
    if not new:
        return []
    admins = (
        await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))
    ).scalars().all()
    if not admins:
        return []
    names = ", ".join(f"{item['model']} ({item['trace_count']} traces)" for item in new[:10])
    more = f" and {len(new) - 10} more" if len(new) > 10 else ""
    for admin_id in admins:
        await notify_web_user(
            db,
            str(admin_id),
            title=f"{len(new)} model(s) ran without a price: their cost is missing from Analytics",
            summary=(
                f"In the last {NOTICE_LOOKBACK_DAYS} days: {names}{more}. Add a price in Settings > Usage and quotas > "
                "Model pricing with the date it applies from, then reprice to fill the cost of the traces since."
            ),
            source_label=SOURCE_LABEL,
            extra_data={
                "notification_source": NOTIFICATION_SOURCE,
                "models": [item["model"] for item in new],
                "path": SETTINGS_PATH,
            },
        )
    await db.commit()
    await redis.sadd(NOTIFIED_KEY, *[item["model"] for item in new])
    return [item["model"] for item in new]
