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

"""Platform Health's dry-run mode (#8255, PRD 6.6).

While Settings > Agents and automations > Platform Health has dry-run on, every write of every Platform Health agent
is logged and not executed: its tool writes through a ``dry_run``
:class:`~app.services.health.sandbox.session.SandboxSession` bound to its
turns (``turns.intercepted_agent_turn``), the writes a service makes without a
tool (a consolidation pass, filing a proposal) by asking
:func:`dry_run_active` first and recording what it would have done with
:func:`record_dry_run_write`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DRY_RUN_AUDIT_ACTION = "concierge.dry_run_write"


async def dry_run_active(db: AsyncSession) -> bool:
    """Whether the running work is Platform Health's and dry-run is on."""
    from app.services.health.identity import in_health_run
    from app.services.health.settings import load_health_settings

    if not in_health_run():
        return False
    return bool(getattr(await load_health_settings(db), "dry_run", False))


async def record_dry_run_write(
    db: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
    anchor_user_id: uuid.UUID | None = None,
) -> None:
    """Record a write dry-run kept from happening (flushed; the caller commits).

    Audited as Platform Health, anchored on *anchor_user_id* (whom the write
    concerned) or the administrator the run acts for; logged only when there
    is neither, since an audit row needs a user to hang on.
    """
    from app.services.audit import log_system_audit
    from app.services.health.identity import HEALTH_ACTOR, current_health_principal

    principal = current_health_principal()
    anchor = anchor_user_id or (principal.on_behalf_of_admin_id if principal else None)
    logger.info("Concierge dry-run: %s on %s %s not executed", action[:80], target_type[:40], target_id)
    if anchor is None:
        return
    await log_system_audit(
        db,
        actor=HEALTH_ACTOR,
        anchor_user_id=anchor,
        action=DRY_RUN_AUDIT_ACTION,
        entity_type=target_type[:50],
        entity_id=str(target_id) if target_id is not None else None,
        summary=f"Dry-run: {action[:200]} not executed",
        details={"action": action[:200], **(details or {})},
    )
