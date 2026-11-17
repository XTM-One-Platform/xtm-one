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

"""AI-drafted collaboration instruction of a fleet's connected member (#9162 R5).

The Members view's instruction editor offers the shared ``PromptGenerator``
("Generate with AI" / "Improve with AI"); its ``generate`` callback calls
``POST /fleets/{id}/members/{member_id}/instruction-draft`` (design:
dev-docs/ctem-fleets.md 10.5 and 10.6). The person says what the instruction
should convey; what makes the draft about THIS member is assembled here, from
the stored fleet: its name and description, the orchestrator, the connected
agent or fleet (name and description), the fleet default instruction and the
member's current text, which an "improve" round keeps.

Nothing is saved: the draft goes back to the editor and the ordinary
``PATCH /fleets/{id}/members/{member_id}`` stores it, or not. One call on the
caller's own model, run only on the person's click, behind the AI gate of the
route. A built-in fleet is refused before the model (its members ship with
the platform); a home member is refused too (an instruction belongs to a
connected row, the rule of ``update_member``). No id reaches the model.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_CONNECTED, Fleet, FleetMember
from app.services.fleets import (
    MEMBER_INSTRUCTION_MAX_LENGTH,
    FleetActionRefusedError,
    ensure_builtin_fleet_writable,
)

logger = logging.getLogger(__name__)

#: Longest brief the editor sends: a sentence or three.
INSTRUCTION_BRIEF_MAX_CHARS = 2000
#: Output budget of one draft (an instruction is at most 4000 characters).
DRAFT_MAX_TOKENS = 1200
#: Bound on each description quoted to the model.
_DESCRIPTION_CHARS = 600

# Kept short on purpose (prompt text is budgeted): the context below carries
# the facts, this says what to write and what never to do.
_SYSTEM_PROMPT = (
    "You write the collaboration instruction a fleet of AI agents follows when "
    "it works with one connected agent or fleet: when to call it, what to ask, "
    "what to send and what to expect back. Write only the instruction, in the "
    "second person to the fleet, plain sentences, no greeting, no heading, no "
    "quotes around it. Use only the facts given; never invent a tool, schedule "
    "or figure. Write in the language of the brief. At most 4000 characters."
)


class InstructionDraftError(Exception):
    """The model did not return a usable instruction."""


@dataclass(frozen=True, slots=True)
class InstructionDraft:
    """A drafted instruction; ``truncated`` when still cut after one retry."""

    text: str
    truncated: bool = False


def _excerpt(text: str | None, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def build_instruction_messages(
    *,
    fleet_name: str,
    fleet_description: str | None,
    orchestrator_name: str | None,
    target_kind: str,
    target_name: str,
    target_description: str | None,
    fleet_default: str | None,
    brief: str,
    current_text: str | None = None,
) -> list[ChatMessage]:
    """The conversation the model drafts from; names only, never an id."""
    parts = [f'Fleet: "{fleet_name}".']
    if fleet_description and fleet_description.strip():
        parts.append(f"What the fleet does: {_excerpt(fleet_description, _DESCRIPTION_CHARS)}")
    if orchestrator_name:
        parts.append(f'The fleet\'s orchestrator: "{orchestrator_name}".')
    connected = f'The connected {target_kind}: "{target_name}".'
    if target_description and target_description.strip():
        connected += f" {_excerpt(target_description, _DESCRIPTION_CHARS)}"
    parts.append(connected)
    if fleet_default and fleet_default.strip():
        parts.append(
            "The fleet's default instruction, which this member overrides; stay coherent with it:\n"
            f"{_excerpt(fleet_default, MEMBER_INSTRUCTION_MAX_LENGTH)}"
        )
    if current_text and current_text.strip():
        parts.append(
            "The person already wrote this instruction; keep its points and improve it "
            "rather than replacing it:\n"
            f"{_excerpt(current_text, MEMBER_INSTRUCTION_MAX_LENGTH)}"
        )
    parts.append(f"What the instruction should convey:\n{brief.strip()}")
    return [
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(role="user", content="\n\n".join(parts)),
    ]


def clean_instruction(raw: str | None) -> str:
    """The model's reply as an instruction: fences and wrapping quotes gone, bounded."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if len(text) > MEMBER_INSTRUCTION_MAX_LENGTH:
        text = text[: MEMBER_INSTRUCTION_MAX_LENGTH - 1].rstrip() + "…"
    return text


async def _names(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
    if not ids:
        return {}
    rows = (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()
    return {a.id: a for a in rows}


async def draft_member_instruction(
    db: AsyncSession,
    user_id: uuid.UUID,
    fleet: Fleet,
    member_id: uuid.UUID,
    *,
    brief: str,
    current_text: str | None = None,
    llm: object | None = None,
    model: str | None = None,
) -> InstructionDraft:
    """Draft the collaboration instruction of *fleet*'s member *member_id*; nothing is written.

    Raises :class:`FleetActionRefusedError` (403 built-in fleet, 404 unknown
    member, 400 home member) before the model, and
    :class:`InstructionDraftError` on a failed or empty answer.
    """
    ensure_builtin_fleet_writable(fleet)
    member = (
        await db.execute(select(FleetMember).where(FleetMember.id == member_id, FleetMember.fleet_id == fleet.id))
    ).scalar_one_or_none()
    if member is None:
        raise FleetActionRefusedError(404, "Fleet member not found")
    if member.role != FLEET_ROLE_CONNECTED:
        raise FleetActionRefusedError(400, "A collaboration instruction belongs to a connected agent.")
    agents = await _names(db, [i for i in (member.agent_id, fleet.orchestrator_agent_id) if i is not None])
    if member.agent_id is not None:
        agent = agents.get(member.agent_id)
        target_kind, target_name = "agent", getattr(agent, "name", "") or "agent"
        target_description = getattr(agent, "description", None)
    else:
        target = await db.get(Fleet, member.target_fleet_id) if member.target_fleet_id else None
        target_kind, target_name = "fleet", getattr(target, "name", "") or "connected fleet"
        target_description = getattr(target, "description", None)
    orchestrator = agents.get(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else None
    messages = build_instruction_messages(
        fleet_name=fleet.name,
        fleet_description=fleet.description,
        orchestrator_name=getattr(orchestrator, "name", None),
        target_kind=target_kind,
        target_name=target_name,
        target_description=target_description,
        fleet_default=fleet.default_collaboration_instruction,
        brief=brief,
        current_text=current_text,
    )
    if llm is None:
        from app.llm.router import get_llm_for_user

        llm, model = await get_llm_for_user(db, str(user_id))

    async def _call(max_tokens: int):
        return await llm.chat(messages, model=model, temperature=0.4, max_tokens=max_tokens)  # type: ignore[attr-defined]

    try:
        response, cut = await chat_retrying_a_cut(
            llm, model, DRAFT_MAX_TOKENS, _call, label="Fleet instruction drafter"
        )
    except Exception as exc:
        logger.warning("Instruction draft failed: %s", format_exception_message(exc))
        raise InstructionDraftError("The assistant could not draft the instruction right now. Try again.") from exc
    text = clean_instruction(getattr(response, "content", None))
    if not text:
        raise InstructionDraftError("The assistant returned an empty instruction. Try rephrasing the brief.")
    return InstructionDraft(text=text, truncated=cut)


__all__ = [
    "DRAFT_MAX_TOKENS",
    "INSTRUCTION_BRIEF_MAX_CHARS",
    "InstructionDraft",
    "InstructionDraftError",
    "build_instruction_messages",
    "clean_instruction",
    "draft_member_instruction",
]
