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

"""``organization_onboarding``: the chat interview that completes the organization profile (#11224).

Offered to an administrator's turn only (the profile API is admin-only),
in the default assistant's chat and in an agent chat alike; anyone else is
never offered it and the executor refuses again. ``next`` reads where the
profile stands and the question to ask, ``save`` writes the answers as a new
version (source ``ai_draft``, the provenance ``chat``) and reads it back,
``skip`` records the fields the person does not want to fill now. The rules
are ``services/organization_onboarding.py``'s.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

ORGANIZATION_ONBOARDING_TOOL = "organization_onboarding"
_SOURCE = "builtin:organization_onboarding"
_ACTIONS = ("next", "save", "skip")


def build_organization_onboarding_tool(
    *, user_id: str, caller_is_admin: bool
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The onboarding tool of the primary organization's profile, for *user_id* (an administrator)."""

    async def _onboard(action: str = "next", answers: dict | None = None, fields: list | None = None) -> str:
        from sqlalchemy.exc import IntegrityError

        from app.core.database import async_session
        from app.services import organization_context as org_context
        from app.services import organization_onboarding as onboarding

        verb = str(action or "next").strip().lower()
        if verb not in _ACTIONS:
            return f"Unknown action {action!r}: use one of {', '.join(_ACTIONS)}."
        if verb != "next" and not caller_is_admin:
            return "Refused: only an administrator can change the organization profile."
        try:
            async with async_session() as db:
                row = await org_context.current_version(db)
                profile = org_context.parse_profile(row.profile if row else {})
                if verb == "next":
                    return onboarding.render_step(profile, version=row.version if row else None)
                try:
                    if verb == "save":
                        updated = onboarding.apply_answers(profile, dict(answers or {}))
                        saved = [str(path).strip() for path in (answers or {}) if str(path).strip() != "sub_sector"]
                    else:
                        updated = onboarding.skip_fields(profile, list(fields or []))
                        saved = []
                except onboarding.OnboardingError as exc:
                    return f"Not saved: {exc}"
                # PIR ids answered in the chat are stored with their OpenCTI names (#11589).
                from app.services.organization_context_draft import name_saved_pirs

                updated = await name_saved_pirs(db, user_id=uuid.UUID(user_id), profile=updated)
                sources: dict[str, Any] = {path: {"source": "chat", "basis": "onboarding interview"} for path in saved}
                new_row = await org_context.save_version(
                    db,
                    updated,
                    source="ai_draft",
                    author_user_id=uuid.UUID(user_id),
                    sources=org_context.accepted_sources(sources),
                )
                await db.commit()
                # The read-back: the version as stored, never the value sent.
                await db.refresh(new_row)
                stored = org_context.parse_profile(new_row.profile)
                return onboarding.render_step(stored, version=new_row.version, saved=saved or None)
        except IntegrityError:
            return 'Not saved: another version was saved at the same time. Call action="next" and ask again.'
        except Exception:
            logger.warning("Organization onboarding failed", exc_info=True)
            return "Error: the organization profile could not be read or saved right now. Say so; nothing was changed."

    executor = CallableToolExecutor()
    executor.register(ORGANIZATION_ONBOARDING_TOOL, _onboard)
    definition = ToolDefinition(
        name=ORGANIZATION_ONBOARDING_TOOL,
        description=(
            "Complete the organization profile with the user, one question at a time. action=next returns where "
            "the profile stands and the field(s) to ask for next; action=save writes answers {field path: value} "
            "as a new version and reads it back; action=skip records fields the user does not fill now. Ask only "
            "what next names, propose a value when you can infer it, save only what the user confirmed, and stop "
            "with a one-line summary when next says the onboarding is complete."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS), "description": "next (default), save or skip."},
                "answers": {
                    "type": "object",
                    "description": 'save: contract path -> value, e.g. {"currency": "EUR", '
                    '"loaded_hourly_rate": 95} or {"risk_appetite.approval_threshold": 50000}.',
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'skip: the contract paths the user skips, e.g. ["ticketing"].',
                },
            },
            "required": [],
        },
        source=_SOURCE,
        priority=0,
    )
    return [definition], executor
