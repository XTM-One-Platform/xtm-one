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

"""A workspace's knowledge: its Sources, its linked skills, a summary.

What every agent working in a workspace receives - instructions, sources,
bound knowledge bases, linked skills - is decided per turn in
``services.workspace_knowledge``. This router is how people manage it:

* the Sources are a knowledge base the workspace owns. Its CONTENT is written
  through the ordinary knowledge-base routes, under the workspace's tier
  (``helpers.knowledge_base_write_refusal``); here are only the two things
  those routes cannot do - create it, and add a file from a conversation of
  this workspace;
* linked skills follow the rules of bound knowledge bases
  (``services.workspaces.relink_workspace_skills``);
* the summary feeds the knowledge panel.

Reading needs to see the workspace; writing needs its config tier
(``get_manageable_entity``). Gated like the workspace CRUD: the licence only,
except the attachment ingestion, which queues embedding work.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed, require_ai_enabled
from app.api.v1.helpers import (
    can_manage,
    enqueue_document_processing,
    get_manageable_entity,
    get_user_group_ids,
    parse_uuid_list,
)
from app.api.v1.schemas.workspace import (
    WorkspaceConversationCounts,
    WorkspaceKnowledgeSummary,
    WorkspaceSkillRef,
    WorkspaceSkillsUpdate,
    WorkspaceSourceAdded,
    WorkspaceSourceFromAttachment,
    WorkspaceSourcesCounts,
    WorkspaceSourcesKnowledgeBase,
)
from app.api.v1.workspaces import get_visible_workspace
from app.core.database import get_db
from app.models.chat import Conversation
from app.models.knowledge_base import KnowledgeDocument
from app.models.user import User
from app.models.workspace import Workspace
from app.services import kb_documents
from app.services.audit import log_audit
from app.services.workspace_conversation_index import index_counts
from app.services.workspaces import (
    WORKSPACE_KB_SOURCES,
    WorkspaceActionRefusedError,
    count_visible_links,
    ensure_workspace_kb,
    relink_workspace_skills,
    visible_linked_skills,
    workspace_kb,
)

router = APIRouter()

_LABEL = "Workspace"


async def _manageable(db: AsyncSession, user: User, workspace_id: str) -> Workspace:
    return await get_manageable_entity(
        db, Workspace, user, workspace_id, action="edit", entity_label=_LABEL
    )


@router.get("/{workspace_id}/knowledge", response_model=WorkspaceKnowledgeSummary)
async def get_workspace_knowledge(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """What this workspace gives its agents, counted for the caller."""
    group_ids = await get_user_group_ids(db, user.id)
    workspace = await get_visible_workspace(db, user, workspace_id, group_ids)
    sources = await workspace_kb(db, workspace.id, WORKSPACE_KB_SOURCES)
    counts = WorkspaceSourcesCounts()
    if sources is not None:
        rows = (
            await db.execute(
                select(KnowledgeDocument.status, func.count())
                .where(KnowledgeDocument.knowledge_base_id == sources.id)
                .group_by(KnowledgeDocument.status)
            )
        ).all()
        by_status = {status_: int(n) for status_, n in rows}
        counts = WorkspaceSourcesCounts(
            count=sum(by_status.values()),
            ready=by_status.get("ready", 0),
            pending=by_status.get("pending", 0) + by_status.get("processing", 0),
            failed=by_status.get("error", 0),
        )
    bound, skills = await count_visible_links(
        db, workspace, user.id, group_ids, is_admin=user.is_admin
    )
    return WorkspaceKnowledgeSummary(
        conversations=WorkspaceConversationCounts(**await index_counts(db, workspace.id)),
        instructions=workspace.instructions or "",
        can_manage=can_manage(user, workspace, group_ids),
        sources_knowledge_base_id=str(sources.id) if sources is not None else None,
        sources=counts,
        skills=skills,
        knowledge_bases=bound,
    )


@router.post("/{workspace_id}/sources", response_model=WorkspaceSourcesKnowledgeBase)
async def ensure_workspace_sources(
    workspace_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The workspace's Sources knowledge base, created on first call.

    Its documents and sync sources are then managed through
    ``/api/v1/knowledge-bases/{knowledge_base_id}/…`` under this workspace's
    tier. Idempotent: a second call returns the same knowledge base, and only
    the call that created it is audited.
    """
    workspace = await _manageable(db, user, workspace_id)
    existed = await workspace_kb(db, workspace.id, WORKSPACE_KB_SOURCES) is not None
    kb = await ensure_workspace_kb(db, workspace, WORKSPACE_KB_SOURCES)
    if not existed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="workspace",
            entity_id=workspace.id,
            entity_name=workspace.name,
            summary=f"Created the sources of workspace '{workspace.name}'",
            details={"knowledge_base_id": str(kb.id)},
            request=request,
        )
    await db.commit()
    return WorkspaceSourcesKnowledgeBase(knowledge_base_id=str(kb.id))


@router.post(
    "/{workspace_id}/sources/from-attachment",
    response_model=WorkspaceSourceAdded,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_source_from_attachment(
    workspace_id: str,
    body: WorkspaceSourceFromAttachment,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Add a file attached in one of this workspace's conversations as a source.

    The caller must be able to read the file, and the file must belong to a
    conversation filed in THIS workspace: a manager cannot turn a file from
    elsewhere - another workspace's thread, a colleague's - into content every
    member of this one reads. Both refusals are the same 404, so the answer
    tells nothing about a file the caller may not reach.
    """
    from app.services.file_access import file_not_found, load_readable_file
    from app.services.file_storage import file_storage

    workspace = await _manageable(db, user, workspace_id)
    attachment = await load_readable_file(db, body.file_attachment_id, user)
    filed_here = attachment.conversation_id is not None and (
        await db.execute(
            select(Conversation.workspace_id).where(Conversation.id == attachment.conversation_id)
        )
    ).scalar_one_or_none() == workspace.id
    if not filed_here:
        raise file_not_found()
    try:
        data = await file_storage.download(attachment.s3_object_name)
    except Exception:
        raise HTTPException(
            status_code=404, detail="The file's content could not be loaded."
        ) from None

    kb = await ensure_workspace_kb(db, workspace, WORKSPACE_KB_SOURCES)
    doc = await kb_documents.add_file_document(
        db,
        kb,
        filename=attachment.filename or "attachment",
        data=data,
        activity_details=f"Added '{(attachment.filename or 'attachment')[:200]}' from a conversation",
    )
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Added a conversation file to the sources of workspace '{workspace.name}'",
        details={"file_attachment_id": str(attachment.id), "document_id": str(doc.id)},
        request=request,
    )
    doc_id = str(doc.id)
    await db.commit()
    await enqueue_document_processing(doc_id)
    return WorkspaceSourceAdded(knowledge_base_id=str(kb.id), document_id=doc_id)


async def _skill_refs(db: AsyncSession, workspace: Workspace, user: User, group_ids: list[str]):
    return [
        WorkspaceSkillRef(skill_id=str(s.id), name=s.name, description=s.description or "")
        for s in await visible_linked_skills(
            db, workspace, user.id, group_ids, is_admin=user.is_admin
        )
    ]


@router.get("/{workspace_id}/skills", response_model=list[WorkspaceSkillRef])
async def list_workspace_skills(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The skills linked to this workspace that the caller can see."""
    group_ids = await get_user_group_ids(db, user.id)
    workspace = await get_visible_workspace(db, user, workspace_id, group_ids)
    return await _skill_refs(db, workspace, user, group_ids)


@router.put("/{workspace_id}/skills", response_model=list[WorkspaceSkillRef])
async def set_workspace_skills(
    workspace_id: str,
    body: WorkspaceSkillsUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Replace the skills linked to this workspace (the bound knowledge bases'
    rules: an added skill the caller cannot see is a 404 for that id, a link
    the caller cannot see is kept)."""
    workspace = await _manageable(db, user, workspace_id)
    group_ids = await get_user_group_ids(db, user.id)
    wanted = set(parse_uuid_list(body.skill_ids, "skill"))
    try:
        added, removed = await relink_workspace_skills(
            db, workspace, user.id, group_ids, is_admin=user.is_admin, wanted=wanted
        )
    except WorkspaceActionRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Updated skills linked to workspace '{workspace.name}'",
        details={
            "added_skill_ids": sorted(str(s) for s in added),
            "removed_skill_ids": sorted(str(s) for s in removed),
        },
        request=request,
    )
    await db.commit()
    return await _skill_refs(db, workspace, user, group_ids)
