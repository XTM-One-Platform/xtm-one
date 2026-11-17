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

"""Workspaces in the embedded chat (``@filigran/chatbot``).

The chat panel OpenCTI, OpenAEV and OpenCRQ embed groups its conversations by
workspace the way the XTM One web chat does: it lists the caller's
workspaces, creates and renames one, deletes one, and files a conversation
into one (or out of it). These routes are that surface, served under the
embedded chat's own prefix (``/api/v1/platform/chat``) so a host proxies them
next to ``/chat/sessions``.

Nothing here decides a rule. Every route runs the REST handler that already
decides it - ``api/v1/workspaces.py`` for a workspace, ``PATCH
/chat/conversations/{id}`` for filing - so the embedded chat, the web chat and
the agent-builder tools refuse the same things with the same words, and write
the same audit entries. The one rule added here is the embedded chat's own: a
conversation is reachable only when it is the caller's AND a platform-channel
one, exactly as ``DELETE /chat/sessions/{id}`` already requires.

Managing a workspace runs no model, so the workspace routes are gated on the
licence alone (``require_agentic_ai_licensed``), like ``/workspaces``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.deps import get_current_user, get_db, require_agentic_ai_licensed
from app.api.v1 import chat as chat_api
from app.api.v1 import workspaces as workspaces_api
from app.api.v1.schemas.workspace import WorkspaceCreate, WorkspaceResponse, WorkspaceUpdate
from app.models.user import User

router = APIRouter()


class PlatformWorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class PlatformWorkspaceUpdate(BaseModel):
    """What the chat panel changes on a workspace: its name and description.

    Sharing, company-managed status and ownership stay on the XTM One
    Workspaces page, where the people they reach are shown before a save.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class PlatformWorkspace(BaseModel):
    """A workspace as the chat panel reads it - nothing more.

    These routes are proxied by third-party hosts (OpenCTI, OpenAEV, OpenCRQ):
    who a workspace is shared with, and its owner's name, stay on the XTM One
    Workspaces page. ``is_own`` says which ones are the caller's, since the
    panel knows no XTM One user id.
    """

    id: str
    user_id: str | None
    name: str
    is_default: bool
    is_archived: bool
    can_manage: bool
    is_own: bool

    @classmethod
    def of(cls, workspace: WorkspaceResponse, user: User) -> PlatformWorkspace:
        return cls(
            id=workspace.id,
            user_id=workspace.user_id,
            name=workspace.name,
            is_default=workspace.is_default,
            is_archived=workspace.is_archived,
            can_manage=workspace.can_manage,
            is_own=workspace.user_id == str(user.id),
        )


class PlatformWorkspaceList(BaseModel):
    workspaces: list[PlatformWorkspace]


class PlatformChatSessionUpdated(BaseModel):
    conversation_id: str
    title: str
    workspace_id: str | None = None


@router.get("/chat/workspaces")
async def list_platform_chat_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> PlatformWorkspaceList:
    """The caller's workspaces, archived ones left out (they take no conversation).

    ``can_manage`` says which ones the caller may file into, and ``is_own``
    which ones are the caller's: the panel knows no XTM One user id, and a
    colleague's default shared with the caller is not the caller's default.
    The list creates the caller's default on first use, like ``GET
    /workspaces``.
    """
    workspaces = await workspaces_api.list_workspaces(
        include_archived=False,
        with_contents=False,
        scope="visible",
        user=user,
        db=db,
        _gate=None,
    )
    return PlatformWorkspaceList(workspaces=[PlatformWorkspace.of(w, user) for w in workspaces])


@router.post("/chat/workspaces", status_code=status.HTTP_201_CREATED)
async def create_platform_chat_workspace(
    body: PlatformWorkspaceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> PlatformWorkspace:
    """Create a personal workspace for the caller."""
    created = await workspaces_api.create_workspace_endpoint(
        body=WorkspaceCreate(name=body.name, description=body.description),
        request=request,
        user=user,
        db=db,
        _gate=None,
    )
    return PlatformWorkspace.of(created, user)


@router.patch("/chat/workspaces/{workspace_id}")
async def update_platform_chat_workspace(
    workspace_id: str,
    body: PlatformWorkspaceUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> PlatformWorkspace:
    """Rename a workspace, or change its description (whoever may manage it)."""
    updated = await workspaces_api.update_workspace(
        workspace_id=workspace_id,
        body=WorkspaceUpdate.model_validate(body.model_dump(exclude_unset=True)),
        request=request,
        user=user,
        db=db,
        _gate=None,
    )
    return PlatformWorkspace.of(updated, user)


@router.delete("/chat/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform_chat_workspace(
    workspace_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> Response:
    """Delete a workspace: its conversations are kept, in no workspace.

    Refused for the default workspace and while it holds work items, as on the
    Workspaces page.
    """
    await workspaces_api.delete_workspace(
        workspace_id=workspace_id, request=request, user=user, db=db, _gate=None
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/chat/sessions/{conversation_id}")
async def update_platform_chat_session(
    conversation_id: str,
    body: chat_api.ConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlatformChatSessionUpdated:
    """Rename an embedded-chat conversation, or file it into a workspace.

    Only the caller's own platform-channel conversations, the ones the history
    menu lists: anything else is the same 404, as on ``DELETE``. The change
    itself is the web chat's ``PATCH /chat/conversations/{id}``: filing takes
    the manage right on the workspace and refuses an archived one.

    The body is the web chat's own ``ConversationUpdate``, so a blank or
    over-long title is a 422 here too; an explicit ``null`` ``workspace_id``
    unfiles. A request that changes nothing writes nothing.
    """
    from app.api.v1.helpers import parse_conversation_id
    from app.api.v1.platform import PLATFORM_CHAT_CHANNEL
    from app.models.chat import Conversation

    conv_uuid = parse_conversation_id(conversation_id)
    owned = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
                Conversation.channel == PLATFORM_CHAT_CHANNEL,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to update: send title or workspace_id.")
    current_workspace = str(owned.workspace_id) if owned.workspace_id else None
    unchanged = ("title" not in fields or body.title == owned.title) and (
        "workspace_id" not in fields or (body.workspace_id or None) == current_workspace
    )
    if unchanged:
        return PlatformChatSessionUpdated(
            conversation_id=str(owned.id), title=owned.title, workspace_id=current_workspace
        )
    updated = await chat_api.update_conversation(
        conversation_id=conversation_id, body=body, user=user, db=db
    )
    return PlatformChatSessionUpdated(
        conversation_id=updated.id, title=updated.title, workspace_id=updated.workspace_id
    )
