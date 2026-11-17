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

"""Browser extension stateless execution endpoint.

Provides ``POST /api/v1/extension/execute-task`` — a thin proxy that
resolves a browser-extension agent by slug, runs it statelessly via
``run_agent_chat()``, and returns the parsed JSON result without
persisting any Conversation or Message rows.

This reuses the full Agent framework (persona, tools, knowledge bases,
Langfuse tracing, quota enforcement) while keeping utility tasks
ephemeral.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_chat_enabled
from app.api.v1.helpers import get_user_group_ids, user_visible_filter
from app.api.v1.schemas.extension import ExtensionTaskRequest
from app.core.redaction import known_secrets_scoped, redact_known_secrets_for_display
from app.core.utils import format_exception_message
from app.llm.router import is_provider_unavailable_error
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_chat import run_agent_chat
from app.services.agentic_executions import (
    SOURCE_EXTENSION,
    record_agentic_execution,
)
from app.services.quota import (
    QuotaExceededError,
    check_agentic_quota,
    increment_agentic_usage,
    refund_agentic_usage_for_failure,
    refund_agentic_usage_for_provider_outage,
    seconds_until_period_reset,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Only agents whose slug starts with this prefix can be invoked via
# the extension endpoint.  This prevents callers from reaching
# internal agents (e.g. opencti-assistant) through a stateless
# side-channel.
_EXTENSION_SLUG_PREFIX = "browser-"


@router.post("/execute-task")
@known_secrets_scoped()
async def execute_extension_task(
    body: ExtensionTaskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Execute a browser-extension task statelessly.

    1. Validate the agent slug starts with ``browser-``.
    2. Enforce per-user agentic quota (429 if exceeded).
    3. Resolve the agent by slug from the database.
    4. Call ``run_agent_chat()`` with empty history and no persistence.
    5. Parse the agent's text response as JSON and return it.
    """
    # ── Slug restriction ─────────────────────────────────────────────
    if not body.agent_slug.startswith(_EXTENSION_SLUG_PREFIX):
        raise HTTPException(
            status_code=403,
            detail="This endpoint only supports browser-extension agents.",
        )

    # ── Resolve agent (three-tier visibility) ───────────────────────
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(Agent).where(
            Agent.slug == body.agent_slug,
            Agent.enabled.is_(True),
            user_visible_filter(Agent, user.id, user_group_ids),
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{body.agent_slug}' not found or disabled.",
        )

    # ── Quota check + increment (after resolution to avoid charging for 404s)
    try:
        await check_agentic_quota(db, user.id)
        await increment_agentic_usage(user.id, db=db, agent_id=agent.id)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(seconds_until_period_reset(exc.period))},
        ) from exc

    # Execution telemetry: recorded here rather than by ``enforce_agentic_quota``
    # because the ``run_agent_chat`` call below passes ``meter_quota=False``.
    await record_agentic_execution(SOURCE_EXTENSION)

    # ── Stateless execution ─────────────────────────────────────────
    try:
        result_text, _ = await run_agent_chat(
            db,
            str(user.id),
            agent,
            history=[],
            user_message=body.content,
            allow_handover=False,
            meter_quota=False,  # already checked + incremented above
            max_tokens_override=body.max_tokens,
        )
    except Exception as e:
        logger.error(
            "Extension task '%s' failed for user %s: %s",
            body.agent_slug,
            user.id,
            format_exception_message(e),
            exc_info=True,
        )
        # The task spent an agentic action before it ran and delivered
        # nothing, so credit that action back. Free when the LLM could not be
        # called at all (provider unreachable / down / not configured — nothing
        # to ration), otherwise from this agent's bounded failure pool. Neither
        # helper raises, so the 500 below is what the caller sees either way.
        if is_provider_unavailable_error(e):
            await refund_agentic_usage_for_provider_outage(
                user.id, agent.id, db=db, reason="extension task"
            )
        else:
            await refund_agentic_usage_for_failure(
                user.id, agent.id, db=db, reason="extension task"
            )
        raise HTTPException(
            status_code=500,
            detail=f"Error executing task '{body.agent_slug}'",
        ) from e
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # The extension shows the answer: a secret variable value it
    # echoed is the display text.
    result_text = redact_known_secrets_for_display(result_text)

    if not result_text or not result_text.strip():
        return {"data": None, "error": "Agent returned an empty response."}

    # ── Prompt guards on the answer ── ``run_agent_chat`` guards
    # only a metered turn, and this one was metered above. A no-op unless the
    # security option turns it on.
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE, guard_model_output

    result_text = await guard_model_output(
        result_text,
        streamed=False,
        user_id=str(user.id),
        entity_id=agent.id,
        entity_name=body.agent_slug,
        details={"agent_id": str(agent.id), "agent_slug": body.agent_slug, "channel": "browser_extension"},
    )
    if result_text == MODEL_OUTPUT_WITHHELD_MESSAGE:
        return {"data": None, "error": result_text}

    # ── Parse JSON from agent response ──────────────────────────────
    raw = result_text.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Agent '%s' returned non-JSON: %.200s", body.agent_slug, raw
        )
        return {"data": None, "error": "Agent produced invalid JSON."}

    # LLMs sometimes wrap output in {"response": {...}} — unwrap so the
    # client always receives the inner payload directly.
    if isinstance(data, dict) and list(data.keys()) == ["response"]:
        data = data["response"]

    return {"data": data}
