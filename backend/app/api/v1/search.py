# SPDX-License-Identifier: Apache-2.0

"""Global search API: the top bar's search field and its results page.

``GET /search/counts?q=`` answers the matches per entity type (every type,
0 included) in one query; ``GET /search?q=&type=&limit=&offset=&sort_by=&sort_dir=`` one
page, of one type or of every type merged, ranked by default or sorted by
name or last change. Visibility is the one each type's
list route applies (``services/global_search.py``), enforced in SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services import global_search

router = APIRouter()

SearchType = Literal[
    "agent",
    "fleet",
    "assignment",
    "flow",
    "intent",
    "conversation",
    "work_item",
    "knowledge_base",
    "knowledge_document",
    "prompt",
    "skill",
    "template",
    "presentation",
    "video",
    "report",
    "file",
    "integration",
    "mcp_server",
    "custom_tool",
    "user",
    "group",
]


class SearchCountsResponse(BaseModel):
    query: str
    total: int
    counts: dict[str, int]


class SearchHit(BaseModel):
    type: str
    id: str
    name: str
    excerpt: str | None = None
    updated_at: datetime | None = None
    parent_id: str | None = None


class SearchPageResponse(BaseModel):
    query: str
    type: str | None
    total: int
    limit: int
    offset: int
    items: list[SearchHit]


def _query(q: str) -> str:
    term = q.strip()
    if not term:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Type something to search")
    return term


@router.get("/counts", response_model=SearchCountsResponse)
async def search_counts(
    q: str = Query(..., max_length=global_search.MAX_QUERY_LENGTH),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchCountsResponse:
    """Matches per entity type the caller may open, every type listed."""
    term = _query(q)
    ctx = await global_search.build_context(db, user)
    counts = await global_search.count_by_type(db, ctx, term)
    return SearchCountsResponse(query=term, total=sum(counts.values()), counts=counts)


@router.get("", response_model=SearchPageResponse)
async def search(
    q: str = Query(..., max_length=global_search.MAX_QUERY_LENGTH),
    type: SearchType | None = Query(default=None, description="One entity type; omitted: every type merged by rank."),
    limit: int = Query(default=25, ge=1, le=global_search.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    sort_by: Literal["rank", "name", "updated"] = Query(
        default="rank", description="rank (relevance, default), name or updated (last change)."
    ),
    sort_dir: Literal["asc", "desc"] = Query(default="asc", description="Ignored for rank."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchPageResponse:
    """One ranked page of matches: exact name, then name prefix, then the rest."""
    term = _query(q)
    ctx = await global_search.build_context(db, user)
    counts = await global_search.count_by_type(db, ctx, term)
    total = counts[type] if type is not None else min(sum(counts.values()), global_search.MAX_ALL_DEPTH)
    items = await global_search.search_page(
        db,
        ctx,
        term,
        entity_type=type,
        limit=limit,
        offset=offset,
        sort=sort_by,
        descending=sort_dir == "desc",
    )
    return SearchPageResponse(
        query=term,
        type=type,
        total=total,
        limit=limit,
        offset=offset,
        items=[SearchHit(**item) for item in items],
    )
