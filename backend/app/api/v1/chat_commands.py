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

"""Chat slash commands for the web chat (#6590).

``GET /api/v1/chat/commands`` lists the commands the web chat offers (the
composer's ``/`` menu). ``POST /api/v1/chat/commands/run`` answers one: the
text is parsed by the registry (``services/chat_commands``), so a text the
composer would send as a message is a 422 here, never a command.

A command is answered for whoever may send in the conversation: its owner, as
``POST /conversations/{id}/messages`` checks, with the conversation's agent
still visible to them - a revoked share stops the report at once. Before a
conversation exists, the agent must be visible to the caller, as
``POST /conversations`` checks. Nothing is stored, nothing reaches the model,
no agentic quota is charged.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, PrivateAttr, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_chat_enabled
from app.api.v1.helpers import get_user_agent, get_user_group_ids
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.chat import Conversation
from app.models.user import User
from app.services.chat_commands import (
    ARGUMENT_DETAIL,
    COMMAND_CONTEXT,
    SURFACE_WEB,
    ChatCommand,
    ParsedCommand,
    commands_for,
    parse_command,
    usage,
)
from app.services.chat_commands.context_report import ContextReport, build_context_report
from app.services.chat_commands.render import render_context_report, render_help

router = APIRouter()

#: Commands per user per minute: a report is a handful of reads, and the
#: composer sends one per command typed.
CHAT_COMMANDS_PER_MINUTE = 30


class CommandArgumentResponse(BaseModel):
    name: str
    aliases: list[str]
    description: str


class CommandResponse(BaseModel):
    name: str
    description: str
    usage: str
    arguments: list[CommandArgumentResponse]


def _command_response(command: ChatCommand) -> CommandResponse:
    return CommandResponse(
        name=command.name,
        description=command.description,
        usage=usage(command),
        arguments=[
            CommandArgumentResponse(name=a.name, aliases=list(a.aliases), description=a.description)
            for a in command.arguments
        ],
    )


class RunCommandRequest(BaseModel):
    """A command as typed, and the conversation it is about.

    Without ``conversation_id``, the report is about a conversation not yet
    started with ``agent_id`` (the General Assistant when both are absent).
    """

    text: str = Field(min_length=1, max_length=200)
    # Typed so a malformed id is the body's 422, not a 404 from the handler.
    conversation_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None

    _parsed: ParsedCommand = PrivateAttr()

    @model_validator(mode="after")
    def _check(self) -> RunCommandRequest:
        if self.conversation_id and self.agent_id:
            raise ValueError("Pass conversation_id or agent_id, not both.")
        parsed = parse_command(self.text, SURFACE_WEB)
        if parsed is None:
            raise ValueError("Not a command of this chat.")
        self._parsed = parsed
        return self


class HelpResult(BaseModel):
    kind: Literal["help"] = "help"
    commands: list[CommandResponse]


class RunCommandResponse(BaseModel):
    command: str
    argument: str | None = None
    result: Annotated[ContextReport | HelpResult, Field(discriminator="kind")]
    #: The result as Markdown, for a surface that does not render it.
    markdown: str


@router.get("/commands", response_model=list[CommandResponse])
async def list_commands(
    user: User = Depends(get_current_user),
) -> list[CommandResponse]:
    return [_command_response(command) for command in commands_for(SURFACE_WEB)]


@router.post("/commands/run", response_model=RunCommandResponse)
async def run_command(
    body: RunCommandRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
) -> RunCommandResponse:
    await enforce_rate_limit(
        None,
        "chat-command",
        limit=CHAT_COMMANDS_PER_MINUTE,
        window_seconds=60,
        bucket=str(user.id),
    )
    parsed = body._parsed
    command = parsed.command
    argument = parsed.argument.name if parsed.argument else None

    if command.name != COMMAND_CONTEXT:
        commands = commands_for(SURFACE_WEB)
        return RunCommandResponse(
            command=command.name,
            result=HelpResult(commands=[_command_response(c) for c in commands]),
            markdown=render_help(commands),
        )

    conversation: Conversation | None = None
    agent_id: uuid.UUID | None = body.agent_id
    if body.conversation_id:
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == body.conversation_id,
                    Conversation.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        agent_id = conversation.agent_id

    agent = None
    if agent_id:
        # The row, not the argument: a share revoked since the conversation
        # started hides the agent's configuration at once.
        group_ids = await get_user_group_ids(db, user.id)
        agent = await get_user_agent(db, user.id, str(agent_id), group_ids, is_admin=user.is_admin)

    report = await build_context_report(
        db,
        user_id=user.id,
        conversation=conversation,
        agent=agent,
        detail=argument == ARGUMENT_DETAIL,
    )
    return RunCommandResponse(
        command=command.name,
        argument=argument,
        result=report,
        markdown=render_context_report(report),
    )
