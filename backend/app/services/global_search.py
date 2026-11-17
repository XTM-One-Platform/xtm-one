# SPDX-License-Identifier: Apache-2.0

"""Global search: one query over every entity type a caller may open.

The top bar's search field (and its results page) asks one endpoint for the
whole platform instead of N list routes. Each entity type is a
:class:`SearchSource`: the model, the columns a term is matched on and the
SAME visibility predicate the type's own list route applies, so search never
widens what a caller can reach:

* the three-tier entities (own, shared with the caller or one of their
  groups, company-managed unless ``access_restricted``) go through
  :func:`app.api.v1.helpers.user_visible_filter`, library scope - the admin
  bypass of the detail routes is not taken, as the library lists do not take it;
* assignments follow their agent (:func:`assignment_visible_filter`);
* conversations, and the files bound to them, the conversation access rule
  (``files.readable_conversation_ids`` / ``readable_files_filter``);
* work items the work item read tiers (``work_item_visible_clause``);
* knowledge documents their knowledge base;
* reports their owner alone (``reports._assert_readable``);
* intents are the global registry every signed-in user lists;
* people and groups only for an administrator.

A term matches when EVERY word appears, in any case, in the name or the
description. Rows rank exact name first, then a name starting with the term,
then the rest, the last changed first within a rank.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, false, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.chat import Conversation
from app.models.custom_tool import CustomTool
from app.models.file_attachment import FileAttachment
from app.models.fleet import Fleet
from app.models.flow import Flow
from app.models.group import Group
from app.models.integration import Integration
from app.models.intent import Intent
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.mcp_server import MCPServer
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.report import Report
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.video_studio import VideoProject
from app.models.work_item import WorkItem

#: Longest term accepted; a search box, not a document.
MAX_QUERY_LENGTH = 200
#: Most words of a term matched (the rest are ignored).
MAX_WORDS = 8
#: Largest page, and the deepest row the merged "all" view reaches.
MAX_PAGE_SIZE = 100
MAX_ALL_DEPTH = 200
#: Characters of the description returned as the row's context.
EXCERPT_LENGTH = 200

_LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Neutralise ``LIKE`` wildcards in a user term (see ``users.escape_like``)."""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", f"{_LIKE_ESCAPE}%").replace("_", f"{_LIKE_ESCAPE}_")
    )


def search_words(query: str) -> list[str]:
    """The distinct lower-cased words of *query*, at most :data:`MAX_WORDS`."""
    words: list[str] = []
    for word in query[:MAX_QUERY_LENGTH].lower().split():
        if word not in words:
            words.append(word)
    return words[:MAX_WORDS]


@dataclass(frozen=True)
class SearchContext:
    """Who searches: everything a visibility predicate needs, read once."""

    user: User
    group_ids: list[str]
    readable_workspace_ids: list[uuid.UUID]

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


@dataclass(frozen=True)
class SearchSource:
    """One entity type of the global search."""

    type: str
    model: Any
    name: Any
    description: Any | None
    changed: Any
    visible: Callable[[SearchContext], Any]
    #: Column naming the parent the row opens under (a document's knowledge base).
    parent: Any | None = None


def _three_tier(model: Any) -> Callable[[SearchContext], Any]:
    def clause(ctx: SearchContext) -> Any:
        from app.api.v1.helpers import user_visible_filter

        return user_visible_filter(model, ctx.user_id, ctx.group_ids)

    return clause


def _assignments(ctx: SearchContext) -> Any:
    from app.api.v1.helpers import assignment_visible_filter

    return assignment_visible_filter(ctx.user_id, ctx.group_ids)


def _conversations(ctx: SearchContext) -> Any:
    from app.api.v1.files import readable_conversation_ids

    return Conversation.id.in_(readable_conversation_ids(ctx.user_id))


def _files(ctx: SearchContext) -> Any:
    from app.api.v1.files import readable_files_filter

    return readable_files_filter(ctx.user)


def _work_items(ctx: SearchContext) -> Any:
    from app.services.work_item import work_item_visible_clause

    return work_item_visible_clause(ctx.user_id, ctx.readable_workspace_ids)


def _documents(ctx: SearchContext) -> Any:
    from app.api.v1.helpers import user_visible_filter

    return KnowledgeDocument.knowledge_base_id.in_(
        select(KnowledgeBase.id).where(user_visible_filter(KnowledgeBase, ctx.user_id, ctx.group_ids))
    )


def _reports(ctx: SearchContext) -> Any:
    return Report.user_id == ctx.user_id


def _everyone(_ctx: SearchContext) -> Any:
    return literal(True)


def _admins(ctx: SearchContext) -> Any:
    return literal(True) if ctx.is_admin else false()


#: Every searchable type, in the order the results page lists equal counts.
SOURCES: tuple[SearchSource, ...] = (
    SearchSource("agent", Agent, Agent.name, Agent.description, Agent.updated_at, _three_tier(Agent)),
    SearchSource("fleet", Fleet, Fleet.name, Fleet.description, Fleet.updated_at, _three_tier(Fleet)),
    SearchSource(
        "assignment", Assignment, Assignment.name, Assignment.description, Assignment.updated_at, _assignments
    ),
    SearchSource("flow", Flow, Flow.name, Flow.description, Flow.updated_at, _three_tier(Flow)),
    SearchSource("intent", Intent, Intent.name, Intent.description, Intent.updated_at, _everyone),
    SearchSource("conversation", Conversation, Conversation.title, None, Conversation.updated_at, _conversations),
    SearchSource("work_item", WorkItem, WorkItem.title, WorkItem.description, WorkItem.updated_at, _work_items),
    SearchSource(
        "knowledge_base",
        KnowledgeBase,
        KnowledgeBase.name,
        KnowledgeBase.description,
        KnowledgeBase.updated_at,
        _three_tier(KnowledgeBase),
    ),
    SearchSource(
        "knowledge_document",
        KnowledgeDocument,
        KnowledgeDocument.filename,
        None,
        KnowledgeDocument.updated_at,
        _documents,
        parent=KnowledgeDocument.knowledge_base_id,
    ),
    SearchSource("prompt", Prompt, Prompt.name, Prompt.description, Prompt.updated_at, _three_tier(Prompt)),
    SearchSource(
        "skill", AgentSkill, AgentSkill.name, AgentSkill.description, AgentSkill.updated_at, _three_tier(AgentSkill)
    ),
    SearchSource(
        "template",
        TemplateContainer,
        TemplateContainer.name,
        TemplateContainer.description,
        TemplateContainer.updated_at,
        _three_tier(TemplateContainer),
    ),
    SearchSource(
        "presentation",
        Presentation,
        Presentation.title,
        None,
        Presentation.updated_at,
        _three_tier(Presentation),
    ),
    SearchSource("video", VideoProject, VideoProject.title, None, VideoProject.updated_at, _three_tier(VideoProject)),
    SearchSource("report", Report, Report.title, None, Report.updated_at, _reports),
    SearchSource(
        "file", FileAttachment, FileAttachment.filename, FileAttachment.description, FileAttachment.created_at, _files
    ),
    SearchSource(
        "integration",
        Integration,
        Integration.name,
        Integration.description,
        Integration.updated_at,
        _three_tier(Integration),
    ),
    SearchSource("mcp_server", MCPServer, MCPServer.name, None, MCPServer.updated_at, _three_tier(MCPServer)),
    SearchSource(
        "custom_tool",
        CustomTool,
        CustomTool.name,
        CustomTool.description,
        CustomTool.updated_at,
        _three_tier(CustomTool),
    ),
    SearchSource("user", User, User.full_name, User.email, User.updated_at, _admins),
    SearchSource("group", Group, Group.name, Group.description, Group.updated_at, _admins),
)
SOURCES_BY_TYPE: dict[str, SearchSource] = {source.type: source for source in SOURCES}
SEARCH_TYPES: tuple[str, ...] = tuple(SOURCES_BY_TYPE)


def _match(source: SearchSource, words: list[str]) -> Any:
    """Every word, in any case, in the name or the description."""
    clauses = []
    for word in words:
        pattern = f"%{escape_like(word)}%"
        columns = [source.name.ilike(pattern, escape=_LIKE_ESCAPE)]
        if source.description is not None:
            columns.append(source.description.ilike(pattern, escape=_LIKE_ESCAPE))
        clauses.append(or_(*columns))
    return and_(*clauses)


def _where(source: SearchSource, ctx: SearchContext, words: list[str]) -> list[Any]:
    return [source.visible(ctx), _match(source, words)]


def _rank(source: SearchSource, query: str) -> Any:
    """0 for the exact name, 1 for a name starting with the term, else 2."""
    term = query.strip().lower()
    lowered = func.lower(source.name)
    return case(
        (lowered == term, 0),
        (lowered.like(f"{escape_like(term)}%", escape=_LIKE_ESCAPE), 1),
        else_=2,
    )


async def build_context(db: AsyncSession, user: User) -> SearchContext:
    """Read the caller's groups and readable workspaces once per search."""
    from app.api.v1.helpers import get_user_group_ids
    from app.services.workspaces import workspace_scope_tiers

    group_ids = await get_user_group_ids(db, user.id)
    readable, _ = await workspace_scope_tiers(db, str(user.id))
    return SearchContext(user=user, group_ids=group_ids, readable_workspace_ids=list(readable))


async def count_by_type(db: AsyncSession, ctx: SearchContext, query: str) -> dict[str, int]:
    """Matches per type, every type present (0 included), in one round trip."""
    words = search_words(query)
    if not words:
        return dict.fromkeys(SEARCH_TYPES, 0)
    counts = [
        select(func.count())
        .select_from(source.model)
        .where(*_where(source, ctx, words))
        .scalar_subquery()
        .label(source.type)
        for source in SOURCES
    ]
    row = (await db.execute(select(*counts))).one()
    return {source.type: int(row._mapping[source.type] or 0) for source in SOURCES}


def _excerpt(value: str | None) -> str | None:
    if not value:
        return None
    flat = " ".join(value.split())
    return flat if len(flat) <= EXCERPT_LENGTH else flat[: EXCERPT_LENGTH - 1].rstrip() + "…"


SORTS = ("rank", "name", "updated")
"""The orders a page can take: relevance (default), then the name or the last change."""


def _order(source: SearchSource, rank: Any, sort: str, descending: bool) -> list[Any]:
    if sort == "name":
        name = func.lower(source.name)
        return [name.desc() if descending else name.asc(), source.model.id]
    if sort == "updated":
        changed = source.changed.desc() if descending else source.changed.asc()
        return [changed.nulls_last(), source.model.id]
    return [rank, source.changed.desc().nulls_last(), source.model.id]


async def _rows(
    db: AsyncSession,
    source: SearchSource,
    ctx: SearchContext,
    query: str,
    words: list[str],
    *,
    limit: int,
    offset: int,
    sort: str = "rank",
    descending: bool = False,
) -> list[dict[str, Any]]:
    rank = _rank(source, query).label("rank")
    columns = [
        source.model.id.label("id"),
        source.name.label("name"),
        (source.description if source.description is not None else literal(None)).label("description"),
        source.changed.label("changed"),
        (source.parent if source.parent is not None else literal(None)).label("parent_id"),
        rank,
    ]
    statement = (
        select(*columns)
        .where(*_where(source, ctx, words))
        .order_by(*_order(source, rank, sort, descending))
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(statement)
    return [
        {
            "type": source.type,
            "id": str(row.id),
            "name": row.name or "",
            "excerpt": _excerpt(row.description),
            "updated_at": row.changed,
            "parent_id": str(row.parent_id) if row.parent_id is not None else None,
            "rank": row.rank,
        }
        for row in result
    ]


def _sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    changed: datetime | None = item["updated_at"]
    return (item["rank"], -(changed.timestamp() if changed else 0.0), item["id"])


def _merge(items: list[dict[str, Any]], sort: str, descending: bool) -> list[dict[str, Any]]:
    """Order the rows of every type the way each type's query ordered its own."""
    if sort == "name":
        items.sort(key=lambda item: item["id"])
        items.sort(key=lambda item: str(item["name"]).lower(), reverse=descending)
    elif sort == "updated":
        dated = sorted((i for i in items if i["updated_at"] is not None), key=lambda i: i["id"])
        dated.sort(key=lambda item: item["updated_at"], reverse=descending)
        items[:] = dated + sorted((i for i in items if i["updated_at"] is None), key=lambda i: i["id"])
    else:
        items.sort(key=_sort_key)
    return items


async def search_page(
    db: AsyncSession,
    ctx: SearchContext,
    query: str,
    *,
    entity_type: str | None,
    limit: int,
    offset: int,
    sort: str = "rank",
    descending: bool = False,
) -> list[dict[str, Any]]:
    """One page of results: of one type, or of every type merged by rank.

    ``sort`` is one of :data:`SORTS`: ``rank`` (relevance, ``descending``
    ignored), ``name`` or ``updated`` (a row with no date last either way).
    The merged view reads the first ``offset + limit`` rows of each type and
    merges them, so it reaches :data:`MAX_ALL_DEPTH` rows at most; a type's
    own view pages without that bound.
    """
    words = search_words(query)
    if not words:
        return []
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}")
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    if entity_type is not None:
        source = SOURCES_BY_TYPE[entity_type]
        items = await _rows(db, source, ctx, query, words, limit=limit, offset=offset, sort=sort, descending=descending)
    else:
        depth = min(offset + limit, MAX_ALL_DEPTH)
        merged: list[dict[str, Any]] = []
        for source in SOURCES:
            merged.extend(
                await _rows(db, source, ctx, query, words, limit=depth, offset=0, sort=sort, descending=descending)
            )
        items = _merge(merged, sort, descending)[offset : offset + limit]
    for item in items:
        item.pop("rank", None)
    return items
