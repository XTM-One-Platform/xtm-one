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

"""Install-specific investigation source packs.

`investigation_packs` declares what a source *is* and holds the built-in packs.
Those are code — the same on every install, and deliberately ORM-free. A
knowledge base and an MCP server are not: they are rows, with per-install ids
and per-user visibility, so the packs that expose them can only be built
against a session and a caller.

This module is that seam. It returns the built-in packs plus, when the caller
has anything to offer, two more:

* ``knowledge-bases`` — one source per knowledge base the caller can see. One
  source *each* rather than one spanning all of them, so the block says which
  corpus answered and which held nothing. Merged into a single step, a run
  across five corpora that matched in one would read as "found something"
  with no way to tell where, and a run that matched in none would look
  identical to a run against one empty corpus.

* ``mcp-tools`` — one source per MCP server whose owner has *designated* a
  tool for investigation. Never inferred: see ``MCPServer`` for why a tool
  name is not something this code may guess.

* ``disinfo-infrastructure`` — offered to every caller, with passive-DNS
  sources when the caller can see a connected VirusTotal integration (see
  ``investigation_packs.disinfo_infrastructure_pack``).

Neither pack is auto-selected (``auto_select=False``). Both are offered by the
picker and chosen deliberately; a chat-initiated run that skipped the picker
gets the built-ins alone. That is the conservative direction — the cost of not
searching a corpus is a thinner report, while the cost of searching every
corpus by default is every investigation quietly reading everything the caller
can see.

Visibility is the platform's, not this module's: both queries go through
``user_visible_query``, so a corpus or a server appears here exactly when it
appears in its own list endpoint.
"""

from __future__ import annotations

import logging

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services import investigation_packs
from app.services.investigation_packs import (
    ONE_SUBJECT_TYPES,
    OPENCTI_PROVIDER,
    VIRUSTOTAL_PROVIDER,
    SourceDefinition,
    SourcePack,
    UnknownSourcePackError,
    disinfo_infrastructure_pack,
    with_knowledge_bases,
)

logger = logging.getLogger(__name__)

KB_PACK_SLUG = "knowledge-bases"
MCP_PACK_SLUG = "mcp-tools"

#: Ceiling on the sources one dynamic pack contributes.
#:
#: Not a safety limit — the run's iteration budget is that — but a bound on the
#: *plan*: an install with two hundred corpora would otherwise render a picker
#: entry promising two hundred steps. Sources are taken in name order so the
#: cut is stable across runs rather than following whatever the database
#: returned, and the pack's description says the list was capped, because a
#: silently truncated pack would overstate its coverage.
MAX_DYNAMIC_SOURCES = 25


def _kb_pack(rows: list[tuple[str, str]]) -> SourcePack | None:
    """Build the knowledge-base pack from ``(id, name)`` rows, or None."""
    if not rows:
        return None
    capped = len(rows) > MAX_DYNAMIC_SOURCES
    chosen = rows[:MAX_DYNAMIC_SOURCES]
    sources = tuple(
        SourceDefinition(
            name=name,
            # Every subject type, `unknown` included: a corpus is prose, and
            # what it happens to mention is not something a regex over the
            # subject can rule out. A free-text actor name classifies as
            # `unknown`, and that is precisely the subject a corpus answers
            # best — excluding it would make the pack useless where it helps
            # most.
            subject_types=ONE_SUBJECT_TYPES,
            kind="knowledge_base",
            knowledge_base_ids=(kb_id,),
        )
        for kb_id, name in chosen
    )
    description = (
        "Search the knowledge bases you can access. Citations name the "
        "document a passage came from."
    )
    if capped:
        description += (
            f" Showing the first {MAX_DYNAMIC_SOURCES} of {len(rows)} by name."
        )
    return SourcePack(
        slug=KB_PACK_SLUG,
        label="Knowledge bases",
        description=description,
        subject_types=ONE_SUBJECT_TYPES,
        sources=sources,
        # A pack of N sources whose budget is 10 would plan work it cannot
        # do. The orchestrator takes the largest ceiling among the selected
        # packs and never less than the number of planned sources.
        max_iterations=max(10, len(sources)),
        auto_select=False,
    )


def _mcp_pack(rows: list[tuple[str, str, str, str | None]]) -> SourcePack | None:
    """Build the MCP pack from ``(server_id, server_name, tool, argument)``."""
    if not rows:
        return None
    capped = len(rows) > MAX_DYNAMIC_SOURCES
    chosen = rows[:MAX_DYNAMIC_SOURCES]
    sources = tuple(
        SourceDefinition(
            # The tool is in the name because one server can be designated
            # once but sits alongside others, and "GitHub" alone would not say
            # what was called.
            name=f"{server_name} · {tool}",
            subject_types=ONE_SUBJECT_TYPES,
            kind="mcp_tool",
            mcp_server_id=server_id,
            mcp_tool_name=tool,
            mcp_argument=argument or None,
        )
        for server_id, server_name, tool, argument in chosen
    )
    description = (
        "Call the tools designated for investigation on your MCP servers. "
        "Only a tool named by its server's owner is ever called."
    )
    if capped:
        description += (
            f" Showing the first {MAX_DYNAMIC_SOURCES} of {len(rows)} by name."
        )
    return SourcePack(
        slug=MCP_PACK_SLUG,
        label="MCP tools",
        description=description,
        subject_types=ONE_SUBJECT_TYPES,
        sources=sources,
        max_iterations=max(10, len(sources)),
        auto_select=False,
    )


async def _visible_knowledge_base_rows(
    db: AsyncSession, user: User, group_ids: list[str] | None = None
) -> list[tuple[str, str, list]]:
    """``(id, name, tags)`` of every knowledge base the caller can see, by name.

    One read for both the knowledge-base pack and the corpora other packs
    declare, so the two can never disagree about what this caller sees.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.knowledge_base import KnowledgeBase

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    # Reset the inherited sort first: `user_visible_query` ships an ORDER BY
    # created_at DESC, and appending to it makes the name a SECOND key that
    # never decides anything. The plan would then follow creation time, and
    # two installs holding the same corpora would order their steps
    # differently — the opposite of the stability this sort is here for.
    query = (
        user_visible_query(KnowledgeBase, user.id, group_ids)
        .order_by(None)
        .order_by(KnowledgeBase.name)
    )
    result = await db.execute(query)
    return [(str(kb.id), kb.name, list(kb.tags or [])) for kb in result.scalars().all()]


def _declared_knowledge_bases(pack: SourcePack, rows: list[tuple[str, str, list]]) -> SourcePack:
    """``pack`` with the corpora it declares, among those the caller sees."""
    resolved = [
        (selector, [(kb_id, name) for kb_id, name, tags in rows if selector.matches(tags, kb_id)])
        for selector in pack.knowledge_bases
    ]
    return with_knowledge_bases(pack, resolved)


async def expand_declared_knowledge_bases(
    db: AsyncSession, user: User | None, packs: list[SourcePack]
) -> list[SourcePack]:
    """The packs with their declared knowledge bases resolved for ``user``.

    No caller, no corpus: a selector resolves to nothing and the pack says so,
    the posture `resolve_packs` takes for every install-specific source.
    """
    if not any(pack.knowledge_bases for pack in packs):
        return packs
    rows = await _visible_knowledge_base_rows(db, user) if user is not None else []
    return [_declared_knowledge_bases(pack, rows) for pack in packs]


async def _designated_mcp_tools(
    db: AsyncSession, user: User, group_ids: list[str] | None = None
) -> list[tuple[str, str, str, str | None]]:
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.mcp_server import MCPServer

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    query = (
        user_visible_query(MCPServer, user.id, group_ids)
        .where(MCPServer.enabled.is_(True))
        .where(MCPServer.investigation_tool_name.isnot(None))
        .where(MCPServer.investigation_tool_name != "")
        # See `_visible_knowledge_base_rows` on why the sort is reset first.
        .order_by(None)
        .order_by(MCPServer.name)
    )
    result = await db.execute(query)
    return [
        (
            str(srv.id),
            srv.name,
            str(srv.investigation_tool_name),
            srv.investigation_argument,
        )
        for srv in result.scalars().all()
    ]


async def _visible_virustotal_integration(
    db: AsyncSession, user: User, group_ids: list[str] | None = None
) -> tuple[str, str] | None:
    """``(id, name)`` of the VirusTotal integration passive DNS uses, or None.

    Connected and holding a key, under the same three-tier visibility as the
    corpora above — with no admin bypass, so an admin's run never spends a
    colleague's private key. One integration, the first by name: a second
    connected key would only query the same records twice.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.integration import Integration

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    query = (
        user_visible_query(Integration, user.id, group_ids)
        .where(Integration.provider == VIRUSTOTAL_PROVIDER)
        .where(Integration.status == "connected")
        .where(Integration.api_key_enc.isnot(None))
        # See `_visible_knowledge_base_rows` on why the sort is reset first.
        .order_by(None)
        .order_by(Integration.name, Integration.id)
        .limit(1)
    )
    integration = (await db.execute(query)).scalars().first()
    if integration is None:
        return None
    return str(integration.id), integration.name


def only_registered_opencti(query: Select) -> Select:
    """Narrow a visible-integrations query to the OpenCTI a run reads and records through.

    Only the one a registered OpenCTI creates (company-managed,
    ``platform_managed``): it stores no secret, and a per-user token is minted
    for every call, so what the run reads is what the caller may read in
    OpenCTI. An ``api_key`` integration would read as its key's owner instead.
    Applied to ``user_visible_query(Integration, ...)`` (the three-tier
    visibility of the other packs), connected, first by name. The pack offering
    the source and the step querying it select the same row through it.
    """
    from app.models.integration import Integration

    return (
        query.where(Integration.provider == OPENCTI_PROVIDER)
        .where(Integration.auth_type == "platform_managed")
        # access_restricted: honoured - user_visible_query serves a restricted row to its grantees only.
        .where(Integration.is_company_managed.is_(True))
        .where(Integration.status == "connected")
        .order_by(None)
        .order_by(Integration.name, Integration.id)
        .limit(1)
    )


async def visible_opencti_integration(
    db: AsyncSession, user: User, group_ids: list[str] | None = None
) -> tuple[str, str] | None:
    """``(id, name)`` of the OpenCTI the caller can read through this platform, or None."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.integration import Integration

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    query = only_registered_opencti(user_visible_query(Integration, user.id, group_ids))
    integration = (await db.execute(query)).scalars().first()
    if integration is None:
        return None
    return str(integration.id), integration.name


async def resolve_packs(db: AsyncSession, user: User | None) -> dict[str, SourcePack]:
    """Every pack this caller may choose from, keyed by slug.

    A caller of None (a system-initiated run with no user on the row) gets the
    built-ins only. That is not a degradation to paper over: the dynamic packs
    exist because somebody can see a corpus or owns a server, and there is no
    such somebody here. Returning everything instead would query private
    corpora on behalf of nobody.
    """
    # Read through the module, not a name bound at import: the built-in
    # registry is a module attribute that tests replace and that the full
    # feature swaps for a database-backed loader. A `from … import` here would
    # pin whichever dict existed when this module was first imported, and the
    # substitution would silently have no effect.
    packs = dict(investigation_packs.BUILTIN_PACKS)
    if user is None:
        expanded = await expand_declared_knowledge_bases(db, None, list(packs.values()))
        return {pack.slug: pack for pack in expanded}

    from app.api.v1.helpers import get_user_group_ids

    # One group lookup and one read of each resource for every pack below.
    group_ids = await get_user_group_ids(db, user.id)
    resources, kb_rows = await caller_resources(db, user, group_ids)
    kb = _kb_pack([(kb_id, name) for kb_id, name, _tags in kb_rows])
    if kb is not None:
        packs[kb.slug] = kb

    mcp = _mcp_pack(list(resources.mcp_tools))
    if mcp is not None:
        packs[mcp.slug] = mcp

    disinfo = disinfo_infrastructure_pack(resources.virustotal, resources.opencti)
    packs[disinfo.slug] = disinfo

    # The packs users built that this caller can see, resolved with what THIS
    # caller may use: a shared pack never lends its author's access.
    for built in await _visible_built_packs(db, user, resources, group_ids):
        packs[built.slug] = built

    # Last, over every pack built-in or not: the corpora a pack declares are
    # read from the same rows as the knowledge-base pack.
    return {slug: _declared_knowledge_bases(pack, kb_rows) for slug, pack in packs.items()}


async def caller_resources(db: AsyncSession, user: User, group_ids: list[str] | None = None):
    """``(CallerResources, knowledge-base rows)``: what ``user`` may use. The
    one read behind `resolve_packs` and a built pack's preview, so the preview
    never shows a source the run would not use."""
    from app.api.v1.helpers import get_user_group_ids
    from app.services.investigation_pack_builder import CallerResources

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    resources = CallerResources(
        virustotal=await _visible_virustotal_integration(db, user, group_ids),
        opencti=await visible_opencti_integration(db, user, group_ids),
        mcp_tools=await _designated_mcp_tools(db, user, group_ids),
    )
    return resources, await _visible_knowledge_base_rows(db, user, group_ids)


def with_caller_corpora(pack: SourcePack, kb_rows: list[tuple[str, str, list]]) -> SourcePack:
    """``pack`` with the corpora it declares among the caller's ``kb_rows``."""
    return _declared_knowledge_bases(pack, kb_rows)


async def _visible_built_packs(
    db: AsyncSession, user: User, resources, group_ids: list[str] | None = None
) -> list[SourcePack]:
    """The built packs ``user`` can see (three-tier, no admin bypass: an
    admin's picker lists what is theirs to run, like the corpora), as packs."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.investigation_pack import InvestigationPack
    from app.models.user import User as UserModel
    from app.services.investigation_pack_builder import PackDefinitionError, build_source_pack, parse_definition
    from app.services.investigation_packs import PackOwner

    if group_ids is None:
        group_ids = await get_user_group_ids(db, user.id)
    # The owner's name in the same read: the picker says whose each pack is.
    query = (
        user_visible_query(InvestigationPack, user.id, group_ids)
        .outerjoin(UserModel, UserModel.id == InvestigationPack.user_id)
        .add_columns(UserModel.full_name)
        .order_by(None)
        .order_by(InvestigationPack.name, InvestigationPack.id)
    )
    packs: list[SourcePack] = []
    for row, owner_name in (await db.execute(query)).all():
        try:
            definition = parse_definition(row.definition)
            packs.append(
                build_source_pack(
                    pack_id=row.id,
                    name=row.name,
                    description=row.description or "",
                    subject_types=[t for t in (row.subject_types or []) if isinstance(t, str)],
                    recommended=bool(row.recommended),
                    max_iterations=int(row.max_iterations or 10),
                    version=int(row.version_number or 1),
                    definition=definition,
                    resources=resources,
                    owner=PackOwner(
                        user_id=str(row.user_id) if row.user_id else None,
                        name=owner_name if row.user_id else None,
                        company_managed=bool(row.is_company_managed),
                    ),
                )
            )
        except (PackDefinitionError, ValueError) as exc:
            # A row this build cannot run (written by a newer one, or
            # damaged): left out of the picker, never half-run.
            logger.warning("built investigation pack %s is not runnable: %r", row.id, str(exc)[:300])
    return packs


async def resolve_selected(
    db: AsyncSession, user: User | None, slugs: list[str]
) -> list[SourcePack]:
    """The caller's chosen packs, in the caller's order.

    Order is preserved because ``plan_for`` attributes a shared source to the
    first pack carrying it, so the selection order decides attribution.

    An unresolvable slug raises rather than being dropped. A dynamic pack can
    genuinely vanish between the picker and the run — the last corpus deleted,
    a designation withdrawn — and quietly continuing without it would persist
    a ``source_pack_slugs`` that claims coverage the run never had.
    """
    available = await resolve_packs(db, user)
    missing = [s for s in slugs if s not in available]
    if missing:
        raise UnknownSourcePackError(
            f"unknown source pack(s) {sorted(missing)}; available: {sorted(available)}"
        )
    return [available[s] for s in slugs]
