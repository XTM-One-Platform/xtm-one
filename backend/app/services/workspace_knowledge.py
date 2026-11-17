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

"""What an agent turn receives from the workspace it runs in.

The ONE place that decides it, for every surface - web chat, the General
Assistant, consulted and delegated agents, channels and flows - so they cannot
drift apart (spec 032 FR-025). A turn in a conversation filed in a workspace
receives, for the person it acts for:

* the workspace's instructions;
* its Sources: inline while they fit a share of the model's context window,
  searched otherwise - never a partial set (:func:`sources_for_turn`);
* the knowledge bases bound to it that the person can see, read-only;
* the skills linked to it that the person can see.

It fails closed (FR-024, FR-026): an anonymous participant, an unknown user,
or a person who cannot see the workspace gets nothing. An archived workspace
still serves the conversations already filed in it - archiving hides a
workspace from pickers, it does not retro-unfile threads.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeDocument
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

#: Bounds of ``PlatformSettings.workspace_sources_context_percent``.
SOURCES_CONTEXT_PERCENT_MIN = 5
SOURCES_CONTEXT_PERCENT_MAX = 40

#: Hard ceiling on the instructions, enforced by the API schemas.
WORKSPACE_INSTRUCTIONS_MAX_CHARS = 8000


@dataclass(frozen=True)
class WorkspaceKnowledge:
    """A workspace's contribution to one turn, already filtered for its person."""

    workspace_id: uuid.UUID
    workspace_name: str
    instructions: str
    #: The Sources knowledge base, when the workspace has one.
    sources_kb_id: str | None
    #: Bound knowledge bases the person can see, read-only.
    bound_kb_ids: list[str] = field(default_factory=list)
    #: Linked skills the person can see, and the person's skill access to
    #: re-check them under on every load (built from what the resolution
    #: already loaded, so the turn does not query the person twice).
    skill_ids: list[str] = field(default_factory=list)
    person_access: object | None = None
    #: The Conversations knowledge base, when the workspace has one: the turn
    #: then gets ``search_workspace_conversations``.
    conversations_kb_id: str | None = None


@dataclass(frozen=True)
class TurnSources:
    """How a turn receives the workspace's Sources (research R6)."""

    #: ``full`` (every ready source inline), ``search`` (the knowledge base
    #: joins the turn's read-only set) or ``none`` (nothing ready).
    mode: str
    #: ``[(title, text)]`` in full mode, else empty.
    documents: list[tuple[str, str]] = field(default_factory=list)


#: The embedded platform chat's channel (``api.v1.platform.PLATFORM_CHAT_CHANNEL``).
_PLATFORM_CHAT_CHANNEL = "api"


def private_thread_clause():
    """Conversations whose thread is one person's own: the web chat, the
    embedded platform chat, and Slack / Teams direct messages.

    A group channel thread is not: its answers are read by everyone in the
    channel, members of the workspace or not, so it neither receives a
    workspace's knowledge nor is indexed into it.
    """
    from sqlalchemy import or_

    from app.models.chat import Conversation
    from app.services.channel_continuation import web_listable_clause

    return or_(web_listable_clause(), Conversation.channel == _PLATFORM_CHAT_CHANNEL)


async def load_conversation_workspace_id(
    db: AsyncSession, conversation_id: str | None
) -> uuid.UUID | None:
    """The workspace *conversation_id* is filed in, or None.

    None too for a group channel thread (:func:`private_thread_clause`): its
    answers reach people the workspace was never shared with.

    Guarded twice because it runs at the very start of every turn: an id that
    is not a UUID (anonymous and test callers pass stand-ins) never touches
    the database, and a failing read runs in a savepoint so it cannot poison
    the turn's transaction - a turn never fails because its workspace could
    not be read, it just runs without one.
    """
    from app.core.utils import safe_uuid
    from app.models.chat import Conversation

    conv_id = safe_uuid(str(conversation_id)) if conversation_id else None
    if conv_id is None:
        return None
    try:
        async with db.begin_nested():
            return (
                await db.execute(
                    select(Conversation.workspace_id).where(
                        Conversation.id == conv_id, private_thread_clause()
                    )
                )
            ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("Workspace of conversation %s not read: %r", conv_id, exc)
        return None


async def resolve_turn_workspace_knowledge(
    db: AsyncSession, *, user_id: str | None, workspace_id: uuid.UUID | None
) -> WorkspaceKnowledge | None:
    """What *workspace_id* gives a turn acting for *user_id*, or None (fail closed).

    Visibility is checked against the stored workspace on every turn, so a
    revoked share applies on the next turn (FR-024).
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.services.builtin_tools.skills import SkillAccessContext
    from app.services.workspaces import (
        WORKSPACE_KB_CONVERSATIONS,
        WORKSPACE_KB_SOURCES,
        load_user_for_scope,
        visible_bound_knowledge_bases,
        visible_linked_skills,
        workspace_kb,
    )

    if workspace_id is None:
        return None
    user = await load_user_for_scope(db, user_id)
    if user is None:
        return None
    group_ids = await get_user_group_ids(db, user.id)
    workspace = (
        await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                user_visible_filter(Workspace, user.id, group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if workspace is None:
        return None

    sources = await workspace_kb(db, workspace.id, WORKSPACE_KB_SOURCES)
    conversations = await workspace_kb(db, workspace.id, WORKSPACE_KB_CONVERSATIONS)
    bound = await visible_bound_knowledge_bases(
        db, workspace, user.id, group_ids, is_admin=user.is_admin
    )
    skills = await visible_linked_skills(
        db, workspace, user.id, group_ids, is_admin=user.is_admin
    )
    return WorkspaceKnowledge(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        instructions=workspace.instructions or "",
        sources_kb_id=str(sources.id) if sources is not None else None,
        bound_kb_ids=[str(kb.id) for kb in bound if kb.owner_workspace_id is None],
        skill_ids=[str(skill.id) for skill in skills],
        conversations_kb_id=str(conversations.id) if conversations is not None else None,
        person_access=SkillAccessContext(
            user_id=user.id, group_ids=tuple(group_ids), is_admin=bool(user.is_admin)
        ),
    )


def sources_budget_tokens(context_window: int, percent: int) -> int:
    """The tokens a workspace's sources may fill inline, *percent* clamped."""
    clamped = min(max(percent, SOURCES_CONTEXT_PERCENT_MIN), SOURCES_CONTEXT_PERCENT_MAX)
    return context_window * clamped // 100


async def sources_for_turn(
    db: AsyncSession, kb_id: str | None, *, context_window: int, percent: int
) -> TurnSources:
    """Every ready source inline when they all fit the budget, else search.

    All or nothing: an agent handed a subset in full would answer from it as
    if it were complete. The total is measured in one aggregate query on the
    stored text, and the texts are loaded only when they will be inlined.
    """
    from app.core.utils import safe_uuid
    from app.services.prompt_budget import estimate_tokens, estimate_tokens_for_length

    kb_uuid = safe_uuid(kb_id) if kb_id else None
    if kb_uuid is None:
        return TurnSources(mode="none")
    ready = (
        KnowledgeDocument.knowledge_base_id == kb_uuid,
        KnowledgeDocument.status == "ready",
    )
    count, total_chars = (
        await db.execute(
            select(
                func.count(KnowledgeDocument.id),
                func.coalesce(func.sum(func.length(KnowledgeDocument.content)), 0),
            ).where(*ready)
        )
    ).one()
    if not count:
        return TurnSources(mode="none")
    budget = sources_budget_tokens(context_window, percent)
    if estimate_tokens_for_length(int(total_chars)) > budget:
        return TurnSources(mode="search")
    rows = (
        await db.execute(
            select(KnowledgeDocument.filename, KnowledgeDocument.content)
            .where(*ready)
            .order_by(KnowledgeDocument.created_at.asc(), KnowledgeDocument.id.asc())
        )
    ).all()
    documents = [(title or "Untitled", text or "") for title, text in rows]
    # Re-measured on what was read: a source that grew between the two reads
    # must not push the inline block past the budget.
    if sum(estimate_tokens(text) for _, text in documents) > budget:
        return TurnSources(mode="search")
    return TurnSources(mode="full", documents=documents)


def render_instructions_block(knowledge: WorkspaceKnowledge) -> str:
    """The workspace's instructions as a system-prompt section, or ``""``.

    The person wrote them, so a secret marker in them is display text, never a
    value to restore.
    """
    from app.core.redaction import defuse_secret_markers

    text = (knowledge.instructions or "").strip()
    if not text:
        return ""
    return (
        f"## Workspace instructions\n"
        f"This conversation belongs to the workspace '{_one_line(knowledge.workspace_name)}'. "
        f"Its members set these instructions for every conversation in it:\n\n"
        f"{defuse_secret_markers(text)}"
    )


def render_full_sources_block(knowledge: WorkspaceKnowledge, sources: TurnSources) -> str:
    """The workspace's sources inline, each in its own quarantine block.

    Sources are documents people uploaded or fetched: DATA, never instructions
    (``content_safety.wrap_untrusted``). The boundary is stable so the system
    prompt stays cacheable from one turn to the next.
    """
    from app.core.redaction import defuse_secret_markers
    from app.services.content_safety import wrap_untrusted

    if sources.mode != "full" or not sources.documents:
        return ""
    parts = [
        "## Workspace sources\n"
        f"The workspace '{_one_line(knowledge.workspace_name)}' provides these "
        f"{len(sources.documents)} source document(s), in full. Ground your answers "
        "on them and name the source you use."
    ]
    for title, text in sources.documents:
        parts.append(
            wrap_untrusted(
                defuse_secret_markers(text),
                f"workspace source '{_one_line(title)}'",
                stable=True,
            )
        )
    return "\n\n".join(parts)


def _one_line(text: str) -> str:
    from app.services.content_safety import single_line

    return single_line(text or "")[:200]


async def turn_workspace_knowledge(
    db: AsyncSession,
    *,
    user_id: str | None,
    conversation_id: str | None,
    session_workspace_id: uuid.UUID | None = None,
) -> WorkspaceKnowledge | None:
    """The turn's workspace knowledge, or None - never raises.

    The workspace is *session_workspace_id* when the caller names one (a
    consulted agent, a delegated task, a flow's workspace node), else the
    conversation's. An anonymous participant is answered before the database
    is touched; a failing read runs in a savepoint and yields no knowledge
    rather than a failed turn (fail closed, FR-026).
    """
    from app.core.utils import safe_uuid

    if not user_id or safe_uuid(str(user_id)) is None:
        return None
    workspace_id = session_workspace_id or await load_conversation_workspace_id(
        db, conversation_id
    )
    if workspace_id is None:
        return None
    try:
        async with db.begin_nested():
            return await resolve_turn_workspace_knowledge(
                db, user_id=user_id, workspace_id=workspace_id
            )
    except Exception as exc:
        logger.warning("Workspace %s knowledge not resolved: %r", workspace_id, exc)
        return None


async def turn_sources(
    db: AsyncSession, knowledge: WorkspaceKnowledge, *, llm: object, model: str | None
) -> TurnSources:
    """How this turn receives *knowledge*'s Sources, for the model it runs on.

    The window is the agent loop's own resolution
    (``resolve_effective_context_window``), so the budget and the loop agree on
    how big the model is.
    """
    from app.llm.model_limits import resolve_effective_context_window
    from app.models.platform_settings import PlatformSettings

    if knowledge.sources_kb_id is None:
        return TurnSources(mode="none")
    window = resolve_effective_context_window(
        model=model,
        overrides=getattr(llm, "context_windows", None),
        provider_default=getattr(llm, "context_window", None),
    )
    percent = (
        await db.execute(select(PlatformSettings.workspace_sources_context_percent).limit(1))
    ).scalar_one_or_none()
    return await sources_for_turn(
        db, knowledge.sources_kb_id, context_window=window, percent=percent or 15
    )


def turn_read_kb_ids(knowledge: WorkspaceKnowledge, sources: TurnSources) -> list[str]:
    """The knowledge bases the turn may read for its workspace, never write.

    The bound ones the person can see, and the Sources when they are searched
    rather than inlined.
    """
    ids = list(knowledge.bound_kb_ids)
    if sources.mode == "search" and knowledge.sources_kb_id is not None:
        ids.append(knowledge.sources_kb_id)
    return ids


def conversation_search_tool(knowledge: WorkspaceKnowledge, conversation_id: str | None):
    """The turn's ``search_workspace_conversations`` tool, or None when the
    workspace has no conversation index yet."""
    if knowledge.conversations_kb_id is None:
        return None
    from app.services.builtin_tools.workspace_knowledge import (
        build_workspace_conversation_search_tool,
    )

    return build_workspace_conversation_search_tool(
        workspace_name=knowledge.workspace_name,
        kb_id=knowledge.conversations_kb_id,
        current_conversation_id=conversation_id,
    )
