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

"""``propose_organization_context_change``: a chat request becomes a reviewable proposal (#9484, S-a2).

Wired by ``agent_chat`` for the built-in CTEM Assistant and Platform Health,
and only when the person the turn acts for is an administrator
(``dev-docs/ctem-fleets.md`` section 6.1, "Chat"); anyone else is never
offered it, and the executor refuses a non-administrator again. The tool
stores a pending proposal and never writes a profile: the rules are
``services/organization_context_proposals.py``'s.
"""

from __future__ import annotations

import uuid

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

ORGANIZATION_PROPOSAL_TOOL = "propose_organization_context_change"
_SOURCE = "builtin:organization_proposals"
#: The built-in agents that carry the tool (section 6.1).
ORGANIZATION_PROPOSAL_AGENT_SLUGS: frozenset[str] = frozenset({"ctem-assistant", "platform-health"})


def offers_organization_proposals(agent_slug: str | None, *, is_builtin: bool, caller_is_admin: bool) -> bool:
    """Whether a turn of this agent, for this caller, is offered the proposal tool."""
    return bool(is_builtin and caller_is_admin and (agent_slug or "") in ORGANIZATION_PROPOSAL_AGENT_SLUGS)


def build_organization_proposal_tool(
    *, user_id: str, caller_is_admin: bool, conversation_id: str | None = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The one write-proposal tool of the organization profiles, for *user_id* (an administrator)."""

    async def _propose(organization: str = "", changes: list | None = None, new_tracked: list | None = None) -> str:
        if not caller_is_admin:
            return "Refused: only an administrator can propose a change of the organization profile."
        from app.core.database import async_session
        from app.services import organization_context_proposals as proposals

        try:
            conversation = uuid.UUID(conversation_id) if conversation_id else None
        except ValueError:
            conversation = None
        try:
            async with async_session() as db:
                row = await proposals.propose(
                    db,
                    author_user_id=uuid.UUID(user_id),
                    organization=str(organization or ""),
                    changes=list(changes or []),
                    new_tracked=list(new_tracked or []),
                    conversation_id=conversation,
                )
                await db.commit()
                await db.refresh(row)
                return proposals.render_for_agent(row)
        except proposals.ProposalError as exc:
            return f"Not recorded: {exc}"
        except Exception:
            logger.warning("Organization proposal failed", exc_info=True)
            return "Error: the proposal could not be recorded right now. Say so; nothing was changed."

    from .organization_context_write import SHARED_CONTEXT_CLAUSE

    executor = CallableToolExecutor()
    executor.register(ORGANIZATION_PROPOSAL_TOOL, _propose)
    definition = ToolDefinition(
        name=ORGANIZATION_PROPOSAL_TOOL,
        description=(
            "Propose a change of the organization profile for an administrator to review: field changes "
            "(e.g. add a product to software) or new organizations to track. Nothing is saved until it is "
            "accepted in Settings > Organization. Read get_organization_context first and send the whole new "
            "value of each field, with the basis from the conversation." + SHARED_CONTEXT_CLAUSE
        ),
        input_schema={
            "type": "object",
            "properties": {
                "organization": {
                    "type": "string",
                    "description": "The tracked organization whose profile changes (name or id); omit for your own.",
                },
                "changes": {
                    "type": "array",
                    "description": "Field changes: {field (contract path, e.g. software), value, basis}.",
                    "items": {"type": "object"},
                },
                "new_tracked": {
                    "type": "array",
                    "description": "Organizations to track: {name, relationship (provider|customer|partner|"
                    "subsidiary), criticality (low|medium|high|critical), basis}.",
                    "items": {"type": "object"},
                },
            },
            "required": [],
        },
        source=_SOURCE,
        priority=0,
    )
    return [definition], executor
