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

"""Triggers provisioned in a source platform for ``integration_event`` assignments.

A provider whose platform can be told to call XTM One (a notifier and a trigger
created on its side) registers its pass here. The scheduler tick runs
:func:`maintain_trigger_provisioning`; deleting an assignment runs
:func:`deprovision_assignment`. The generic layers name no provider: each
entry of :data:`_PROVISIONERS` is the provider's own module.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: provider -> module exposing ``maintain(db)`` and ``deprovision(db, assignment)``.
_PROVISIONERS: dict[str, str] = {
    "opencti": "app.services.opencti_trigger_provisioning",
    "openaev": "app.services.openaev_trigger_provisioning",
    # Jira dynamic webhooks (#11036): the poll stays the fallback.
    "jira": "app.services.jira_webhooks",
    # Wordsmith webhook endpoints (#160, slice 3).
    "wordsmith": "app.services.wordsmith_webhooks",
}


def _module(provider: str) -> Any:
    return importlib.import_module(_PROVISIONERS[provider])


async def maintain_trigger_provisioning(db: Any) -> None:
    """One reconciliation pass per provider; one provider never stops another."""
    for provider in _PROVISIONERS:
        try:
            await _module(provider).maintain(db)
        except Exception as exc:
            logger.error(
                "Trigger provisioning pass failed for %s: %s", provider, format_exception_message(exc), exc_info=True
            )
            await db.rollback()


async def deprovision_assignment(db: Any, assignment: Any) -> None:
    """Remove what was provisioned for *assignment* in its source platform (best effort)."""
    config = getattr(assignment, "trigger_config", None)
    provider = config.get("provider") if isinstance(config, dict) else None
    if provider not in _PROVISIONERS:
        return
    try:
        await _module(provider).deprovision(db, assignment)
    except Exception as exc:
        logger.warning(
            "Trigger deprovisioning failed for assignment %s: %s", assignment.id, format_exception_message(exc)
        )
