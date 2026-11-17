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

"""Audit rows of the sandbox workspace: a file put in, a repository checked out, a sweep.

Written on a session of their own, after the change they record committed:
an audit failure is logged and never undoes the change.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

ENTITY_TYPE = "sandbox_workspace"


async def record(
    action: str,
    *,
    user_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> None:
    """An audit row for *user_id*'s action; skipped (logged) for a run with no person."""
    if user_id is None:
        logger.info("Sandbox workspace %s %s (no user to attribute it to): %s", workspace_id, action, summary)
        return
    try:
        from app.core.database import async_session
        from app.models.user import User
        from app.services.audit import log_audit

        async with async_session() as db:
            user = await db.get(User, user_id)
            if user is None:
                return
            await log_audit(
                db,
                user=user,
                action=action,
                entity_type=ENTITY_TYPE,
                entity_id=workspace_id,
                summary=summary,
                details=details,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Could not audit sandbox workspace %s %s: %s", workspace_id, action, format_exception_message(exc))


async def record_system(
    action: str,
    *,
    anchor_user_id: uuid.UUID | None,
    workspace_id: uuid.UUID,
    summary: str,
    details: dict[str, Any] | None = None,
) -> None:
    """An audit row for what the sweep did, anchored on the workspace's owner."""
    if anchor_user_id is None:
        logger.info("Sandbox workspace %s %s by the sweep: %s", workspace_id, action, summary)
        return
    try:
        from app.core.database import async_session
        from app.services.audit import log_system_audit

        async with async_session() as db:
            await log_system_audit(
                db,
                actor="sandbox_workspace_gc",
                anchor_user_id=anchor_user_id,
                action=action,
                entity_type=ENTITY_TYPE,
                entity_id=workspace_id,
                summary=summary,
                details=details,
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Could not audit the sweep of sandbox workspace %s: %s", workspace_id, format_exception_message(exc))
