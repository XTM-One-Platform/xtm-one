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

"""Enterprise knowledge of the organization profile (#11543): the company's own procedures, by tool.

The profile links knowledge bases (SOPs, runbooks, the patch policy) in
``enterprise_knowledge``. They are never preloaded: an agent asks
``get_organization_context(category="enterprise_knowledge", query=...)`` and
reads the passages that answer it, searched only in the linked knowledge
bases THE CALLER can read (``user_visible_filter``, the platform's single
visibility choke point). A linked knowledge base the caller cannot read is
named as such, never searched.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.organization_context import OrganizationProfile

#: Passages one query answers by default, and at most.
DEFAULT_PASSAGES = 5
MAX_PASSAGES = 20

#: ``(query, limit, kb_ids) -> rendered passages``: the platform's knowledge search, injectable in tests.
SearchFn = Callable[[str, int, list[str]], Awaitable[str]]


@dataclass(frozen=True)
class LinkedKnowledgeBase:
    knowledge_base_id: str
    label: str
    kind: str
    readable: bool


async def linked_knowledge_bases(
    db: AsyncSession, profile: OrganizationProfile, *, user_id: uuid.UUID | None, is_admin: bool = False
) -> list[LinkedKnowledgeBase]:
    """The knowledge bases the profile links, each marked readable or not for *user_id*.

    No caller (a system read): nothing is readable. A link to a deleted
    knowledge base is unreadable too.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.knowledge_base import KnowledgeBase

    links = profile.enterprise_knowledge
    if not links:
        return []
    readable: dict[str, str] = {}
    ids: list[uuid.UUID] = []
    for link in links:
        try:
            ids.append(uuid.UUID(link.knowledge_base_id))
        except ValueError:
            continue
    if user_id is not None and ids:
        groups = await get_user_group_ids(db, user_id)
        rows = await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.name).where(
                KnowledgeBase.id.in_(ids), user_visible_filter(KnowledgeBase, user_id, groups, is_admin=is_admin)
            )
        )
        readable = {str(kb_id): name for kb_id, name in rows.all()}
    return [
        LinkedKnowledgeBase(
            knowledge_base_id=link.knowledge_base_id,
            label=readable.get(link.knowledge_base_id) or link.label or link.knowledge_base_id,
            kind=link.kind,
            readable=link.knowledge_base_id in readable,
        )
        for link in links
    ]


def render_links(links: list[LinkedKnowledgeBase]) -> str:
    """The category without a query: what is linked, what the caller can search, how to ask."""
    if not links:
        return "Category enterprise_knowledge: not filled. Do not guess the organization's procedures."
    lines = [f"Category enterprise_knowledge: {len(links)} linked knowledge base(s)."]
    for link in links:
        state = "searchable" if link.readable else "not readable by you"
        lines.append(f"- {link.label} ({link.kind}): {state}")
    lines.append(
        'Ask: get_organization_context(category="enterprise_knowledge", query="the procedure or policy you need").'
    )
    return "\n".join(lines)


async def search_passages(
    links: list[LinkedKnowledgeBase], query: str, *, limit: int = DEFAULT_PASSAGES, search: SearchFn
) -> str:
    """The passages of the readable linked knowledge bases that answer *query*."""
    readable = [link.knowledge_base_id for link in links if link.readable]
    hidden = len(links) - len(readable)
    note = f" {hidden} linked knowledge base(s) are not readable by you and were not searched." if hidden else ""
    if not readable:
        return (
            "Category enterprise_knowledge: no linked knowledge base you can read."
            + note
            + " Say so in your output; do not guess the procedure."
        )
    limit = max(1, min(int(limit or DEFAULT_PASSAGES), MAX_PASSAGES))
    found = await search(query.strip(), limit, readable)
    return f"Category enterprise_knowledge, passages for {query.strip()!r}.{note}\n{found}"
