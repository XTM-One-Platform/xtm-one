# SPDX-License-Identifier: Apache-2.0

"""Shared API helpers – DRY database lookups and response utilities.

All entity lookups accept user-owned, user-shared, group-shared and
company-managed entities.  Visibility = own + user-shared + group-shared +
company-managed, where the company-managed tier can itself be narrowed to the
explicit grants via ``access_restricted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import String as SAString
from sqlalchemy import Text as SAText
from sqlalchemy import and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped

from app.core.json_sql import jsonb_cast_indexed, when_json_parseable
from app.core.utils import safe_uuid, slugify
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.assignment import Assignment, AssignmentRun
from app.models.browser_profile import BrowserProfile
from app.models.channel_bot import ChannelBot
from app.models.custom_dashboard import CustomDashboard
from app.models.custom_tool import CustomTool
from app.models.dashboard_widget_pack import DashboardWidgetPack
from app.models.dataset import Dataset
from app.models.evaluation import Evaluation as _EvaluationModel
from app.models.fleet import Fleet
from app.models.flow import Flow
from app.models.flow_event import FlowEventType
from app.models.flow_run import FlowRun
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.investigation_pack import InvestigationPack
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.nova_rule import NovaRule
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.relay import RelayGroup
from app.models.runner import Runner
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.variable import Variable
from app.models.video_studio import VideoProject
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

#: Every entity carrying the three-tier visibility column set. Membership is
#: what makes ``user_visible_filter`` & friends applicable: they reference
#: ``shared_user_ids``/``access_restricted`` unconditionally on the non-admin
#: branch, so an entity queried through them and missing from this union is a
#: 500 waiting for its first non-admin caller.
type CompanyManagedEntity = (
    Agent
    | Integration
    | KnowledgeBase
    | MCPServer
    | CustomTool
    | ChannelBot
    | Prompt
    | Variable
    | Dataset
    | Runner
    | RelayGroup
    | Flow
    | FlowEventType
    | AgentSkill
    | TemplateContainer
    | NovaRule
    | Presentation
    | Workspace
    | Fleet
    | InvestigationPack
    | VideoProject
    | BrowserProfile
    | CustomDashboard
    | DashboardWidgetPack
)
type VisibilityQueryable = CompanyManagedEntity | _EvaluationModel


@runtime_checkable
class HasCompanyManaged(Protocol):
    """Duck-type protocol for objects exposing ``is_company_managed``."""

    @property
    def is_company_managed(self) -> bool | Mapped[bool]: ...


@runtime_checkable
class HasIsAdmin(Protocol):
    """Duck-type protocol for objects exposing ``is_admin``."""

    @property
    def is_admin(self) -> bool: ...


def fit_agent_slug(base_slug: str, suffix: str = "") -> str:
    """*base_slug* + *suffix*, the base shortened so the result fits ``Agent.slug``.

    A slug that fits is returned unchanged. ``slugify`` can make a slug longer
    than the name it comes from (NFKD decomposes one character into several),
    and a counter lengthens an at-limit one: both answered a 500 on the INSERT.
    """
    room = Agent.__table__.c.slug.type.length - len(suffix)
    if len(base_slug) > room:
        base_slug = base_slug[:room].rstrip("-") or base_slug[:room]
    return f"{base_slug}{suffix}"


async def generate_unique_agent_slug(
    db: AsyncSession, name: str, exclude_id: uuid.UUID | None = None
) -> str:
    """Generate a unique agent slug from a name, appending a counter if needed.

    Lives here rather than in ``agents.py`` because every agent-creation path
    needs it, including the shared import pipeline in ``agent_import.py`` —
    which ``agents.py`` imports, so it cannot import back out of it.
    """
    base_slug = slugify(name)
    slug = fit_agent_slug(base_slug)
    counter = 2
    while True:
        query = select(Agent.id).where(Agent.slug == slug)
        if exclude_id:
            query = query.where(Agent.id != exclude_id)
        result = await db.execute(query)
        if not result.scalar_one_or_none():
            return slug
        slug = fit_agent_slug(base_slug, f"-{counter}")
        counter += 1


def parse_uuid(value: str, *, label: str = "ID") -> uuid.UUID:
    """Parse a string as UUID, raising HTTP 422 on invalid input.

    Use this instead of bare ``uuid.UUID(value)`` in API route handlers to
    avoid 500 errors when clients send non-UUID path/query parameters.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid {label}: {value!r}"
        ) from exc


def parse_path_id(value: str | uuid.UUID | None, *, not_found_detail: str) -> uuid.UUID:
    """Parse a client-supplied id naming the ONE row a route acts on, malformed = unknown.

    A value that is not a UUID names no row, exactly like a well-formed id
    nobody issued, so it raises the 404 the route gives an unknown id
    instead of the 500 of a bare ``uuid.UUID``. A route whose answer to an
    unknown id is not a 404 (an idempotent delete) checks ``safe_uuid``
    itself; a list filter keeps :func:`parse_uuid`'s 422.

    An id that is already a ``uuid.UUID`` is returned as is: ``safe_uuid``
    reads one as malformed, which would turn a lookup that works into a 404.
    """
    if isinstance(value, uuid.UUID):
        return value
    parsed = safe_uuid(value)
    if parsed is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return parsed


def parse_conversation_id(
    value: str | None, *, not_found_detail: str = "Conversation not found"
) -> uuid.UUID:
    """Parse a client-supplied conversation id, answering a malformed one as unknown.

    Conversation ids come back from storage the server does not control - a
    URL, the chatbot's ``localStorage``, an integrator's saved session - so a
    stale one is as likely to be malformed as unknown, and neither names a
    conversation. A value that is not a UUID therefore gets the 404 the route
    gives an unknown id: never the 500 of a bare ``uuid.UUID``, nor a
    422 that a client recovering from a stale id does not expect from a route
    whose answer to that same stale id is 404.

    Only for a route acting on ONE conversation. A conversation id that
    filters a list keeps :func:`parse_uuid`: there, an unknown id is an empty
    page, not an error a malformed one could share.
    """
    return parse_path_id(value, not_found_detail=not_found_detail)


async def owned_conversation(
    db: AsyncSession, user: User, conversation_id: str | None, *, include_archived: bool = True
):
    """The caller's own conversation, or 404 (never 403: it does not confirm another user's exists)."""
    from app.models.chat import Conversation

    query = select(Conversation).where(
        Conversation.id == parse_conversation_id(conversation_id),
        Conversation.user_id == user.id,
    )
    if not include_archived:
        query = query.where(Conversation.is_archived.is_(False))
    conv = (await db.execute(query)).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


# The run-ownership vocabulary shared by the Executions feeds (assignment runs
# and flow runs). "all" is the documented default and means no filter.
RUN_OWNERSHIP_FILTERS = frozenset({"all", "mine", "shared", "company_managed"})


def validate_run_ownership(ownership: str | None) -> str | None:
    """Return the ownership filter to apply, or None for "no filter".

    An unknown value is a 422, never a silently dropped filter: ``?ownership=min``
    must not read as "everybody's runs", and a "delete all matching" must not
    widen to every visible run over a typo.
    """
    # An empty value (``?ownership=``) is an omitted one, not a typo.
    if not ownership or ownership == "all":
        return None
    if ownership not in RUN_OWNERSHIP_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown ownership filter `{ownership}`. Use one of: "
                + ", ".join(sorted(RUN_OWNERSHIP_FILTERS))
                + "."
            ),
        )
    return ownership


#: The internal values ``resolve_list_ownership`` adds to the [DEPRECATED]
#: vocabulary; never accepted from the ``ownership`` parameter itself.
LIST_OWNERSHIP_SEGMENTS = frozenset({"shared_only", "company"})

#: ``?vis=`` segment -> the internal ownership value of ``ownership_scope``.
_VIS_TO_OWNERSHIP: dict[str, str | None] = {
    "mine": "mine",
    "shared": "shared_only",
    "company": "company",
    "all": None,
}


def resolve_list_ownership(vis: str | None, ownership: str | None) -> str | None:
    """The ownership filter of an Operations list: ``vis`` first, else the [DEPRECATED] ``ownership``.

    ``vis`` is the one visibility convention of the list pages
    (``app.services.visibility``): ``mine``, ``shared`` (shared with me, not
    company-managed), ``company`` (company-managed, not mine) or ``all``. It
    is turned into the value ``ownership_clause`` and the run feeds read:
    ``mine``, ``shared_only``, ``company`` or ``None`` (no filter).

    Without ``vis`` the [DEPRECATED] ``ownership`` keeps its exact meaning
    (``shared`` and ``company_managed`` = not mine), validated by
    :func:`validate_run_ownership`. An unknown value of either is a 422.
    """
    from app.services.visibility import to_visibility

    segment = to_visibility(vis)
    if segment is not None:
        return _VIS_TO_OWNERSHIP[segment]
    return validate_run_ownership(ownership)


def parse_uuid_list(ids: list[str] | None, label: str) -> list[uuid.UUID]:
    """Parse and deduplicate a list of UUID strings, raising HTTP 400 on invalid input.

    Returns an empty list when *ids* is None or empty.
    Raises HTTP 400 for any value that is not a valid UUID — including empty strings,
    since the schema uses ``list[str]`` with no upstream UUID validation.
    Deduplicates while preserving input order (via ``dict.fromkeys``) so visibility
    comparisons are safe against duplicate inputs.
    """
    if not ids:
        return []
    try:
        return list(dict.fromkeys(uuid.UUID(i) for i in ids))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail=f"One or more {label} IDs are invalid.",
        ) from None


def require_admin_for_company_managed(
    obj: HasCompanyManaged,
    user: HasIsAdmin,
    detail: str = "Only admins can modify company-managed resources",
) -> None:
    """Raise 403 if *obj*.is_company_managed is truthy and *user* is not admin.

    Works with both SQLAlchemy models (after DB fetch) and Pydantic request
    bodies (during creation), since both expose ``is_company_managed``.
    """
    if bool(getattr(obj, "is_company_managed", False)) and not bool(user.is_admin):
        raise HTTPException(status_code=403, detail=detail)


async def package_origin_knowledge_base_ids(
    db: AsyncSession, knowledge_base_ids: Sequence[uuid.UUID]
) -> set[str]:
    """The ids, among *knowledge_base_ids*, of KBs a skill package materialized.

    One query for a whole list: what list routes serve as
    ``is_package_origin``, the same origin links the write guards read.
    """
    if not knowledge_base_ids:
        return set()
    result = await db.execute(
        select(AgentSkillPackageOrigin.knowledge_base_id).where(
            AgentSkillPackageOrigin.knowledge_base_id.in_(list(knowledge_base_ids))
        )
    )
    return {str(kb_id) for kb_id in result.scalars().all() if kb_id is not None}


async def package_origin_custom_tool_ids(
    db: AsyncSession, custom_tool_ids: Sequence[uuid.UUID]
) -> set[str]:
    """The ids, among *custom_tool_ids*, of custom tools a skill package materialized."""
    if not custom_tool_ids:
        return set()
    result = await db.execute(
        select(AgentSkillPackageOrigin.custom_tool_id).where(
            AgentSkillPackageOrigin.custom_tool_id.in_(list(custom_tool_ids))
        )
    )
    return {str(tool_id) for tool_id in result.scalars().all() if tool_id is not None}


async def is_package_origin_knowledge_base(
    db: AsyncSession, knowledge_base_id: uuid.UUID
) -> bool:
    """Return True when the KB was materialized from a skill package import.

    Such a KB is read-only on every write surface
    (:func:`package_origin_knowledge_base_error`); responses serve the same
    answer as ``is_package_origin`` so the UI never offers those writes.
    """
    return str(knowledge_base_id) in await package_origin_knowledge_base_ids(
        db, [knowledge_base_id]
    )


async def is_package_origin_custom_tool(
    db: AsyncSession, custom_tool_id: uuid.UUID
) -> bool:
    """Return True when the custom tool was materialized from a skill package import.

    Such a tool is read-only on every write surface
    (:func:`package_origin_custom_tool_error`); responses serve the same
    answer as ``is_package_origin`` so the UI never offers those writes.
    """
    return str(custom_tool_id) in await package_origin_custom_tool_ids(
        db, [custom_tool_id]
    )


async def package_origin_custom_tool_error(
    db: AsyncSession, custom_tool_id: uuid.UUID, *, action: str = "modified"
) -> str | None:
    """Return the refusal for writing a package-installed custom tool, else ``None``.

    The one gate every write surface runs: the REST routes through
    :func:`assert_custom_tool_not_package_origin`, the agent-builder tools with
    the string as their tool result - so they refuse the same tools with the
    same sentence. The skill package owns the tool: removing or re-importing
    the package is how it changes. ``action`` is a past participle
    (``"deleted"``).
    """
    if await is_package_origin_custom_tool(db, custom_tool_id):
        return f"Custom tools imported from skill packages cannot be {action}"
    return None


#: The fields of a custom tool its repository source owns
#: (``services/custom_tool_sources.py``): read-only while the tool is synced.
SOURCE_MANAGED_CUSTOM_TOOL_FIELDS = (
    "name",
    "description",
    "icon_url",
    "input_schema",
    "js_function",
    "language",
    "tags",
    "timeout",
    "test_cases",
    "test_code",
)


async def source_managed_custom_tool_error(
    db: AsyncSession, tool: Any, *, action: str = "modified"
) -> str | None:
    """Return the refusal for writing a repository-synced custom tool, else ``None``.

    The gate of every write surface, like :func:`package_origin_custom_tool_error`:
    a tool whose ``source_id`` names an existing source follows its repository,
    so an edit here would be reverted by the next sync. Sharing, enabling and
    hiding stay open; the content fields (:data:`SOURCE_MANAGED_CUSTOM_TOOL_FIELDS`)
    change in the repository, or after detaching the tool. A tool whose source
    is gone is an ordinary tool. ``action`` is a past participle.
    """
    source_id = getattr(tool, "source_id", None)
    if source_id is None:
        return None
    from app.models.custom_tool_source import CustomToolSource

    exists = (
        await db.execute(select(CustomToolSource.id).where(CustomToolSource.id == source_id))
    ).scalar_one_or_none()
    if exists is None:
        return None
    return (
        f"Custom tools synced from a repository cannot be {action}: change the tool in "
        "its repository, or detach it from its source first"
    )


async def assert_custom_tool_not_package_origin(
    db: AsyncSession, custom_tool_id: uuid.UUID, *, action: str = "modified"
) -> None:
    """Raise 403 when a skill package installed the custom tool.

    Thin HTTPException wrapper around :func:`package_origin_custom_tool_error`.
    """
    err = await package_origin_custom_tool_error(db, custom_tool_id, action=action)
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


async def package_origin_knowledge_base_error(
    db: AsyncSession, knowledge_base_id: uuid.UUID, *, action: str = "modified"
) -> str | None:
    """Return the refusal for writing a package-installed knowledge base, else ``None``.

    Same contract as :func:`package_origin_custom_tool_error`, for the KB REST
    routes (:func:`assert_knowledge_base_not_package_origin`), the agent-builder
    tools and the ``store_knowledge`` / ``update_knowledge_document`` tools. The
    package re-materializes the KB, so an edit would be silently reverted.
    """
    if await is_package_origin_knowledge_base(db, knowledge_base_id):
        return f"Knowledge bases imported from skill packages cannot be {action}"
    return None


async def assert_knowledge_base_not_package_origin(
    db: AsyncSession, knowledge_base_id: uuid.UUID, *, action: str = "modified"
) -> None:
    """Raise 403 when a skill package installed the knowledge base.

    Thin HTTPException wrapper around :func:`package_origin_knowledge_base_error`.
    """
    err = await package_origin_knowledge_base_error(
        db, knowledge_base_id, action=action
    )
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


def owning_workspace_id(kb: Any) -> uuid.UUID | None:
    """The workspace that owns *kb*, else ``None``.

    Typed rather than truthy: callers pass stand-in rows too, and an attribute
    a stand-in fabricates must not read as an owner.
    """
    value = getattr(kb, "owner_workspace_id", None)
    return value if isinstance(value, uuid.UUID) else None


async def managing_workspace(db: AsyncSession, kb: Any) -> Workspace | None:
    """The workspace that owns *kb*, loaded, else ``None``."""
    workspace_id = owning_workspace_id(kb)
    return await db.get(Workspace, workspace_id) if workspace_id is not None else None


async def can_manage_entity(
    db: AsyncSession, user: User, entity: Any, user_group_ids: list[str] | None = None
) -> bool:
    """:func:`can_manage`, with a workspace's own knowledge base managed by
    whoever manages the workspace - its own share columns are empty by
    design. The one answer the REST routes, the read routes' manage flag and
    the agent-builder tools share for such a row.
    """
    workspace = await managing_workspace(db, entity)
    return can_manage(user, workspace if workspace is not None else entity, user_group_ids)


async def workspace_managed_knowledge_base_error(
    db: AsyncSession, kb: KnowledgeBase
) -> str | None:
    """Return the refusal for changing a workspace's own knowledge base AS A
    COLLECTION - its settings, sharing, embeddings or existence - else ``None``.

    Those belong to the workspace: the knowledge base is shared through the
    workspace and goes with it. Its CONTENT (documents, sync sources) is
    written through the ordinary knowledge-base routes by whoever manages the
    workspace (:func:`knowledge_base_write_refusal`, ``_assert_kb_manageable``),
    so the formats, limits and sync rules are the knowledge base's own by
    construction.
    """
    workspace_id = owning_workspace_id(kb)
    if workspace_id is None:
        return None
    name = (
        await db.execute(select(Workspace.name).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    return (
        f"Managed by workspace '{name or 'unknown'}'. Change it from the workspace."
    )


async def assert_knowledge_base_not_workspace_managed(
    db: AsyncSession, kb: KnowledgeBase
) -> None:
    """Raise 403 when a workspace owns the knowledge base.

    Thin HTTPException wrapper around :func:`workspace_managed_knowledge_base_error`,
    called on the routes that change the collection itself (its settings, its
    embeddings, its deletion).
    """
    err = await workspace_managed_knowledge_base_error(db, kb)
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


#: Why a knowledge base's content may not be written - the ``reason`` of a
#: :class:`KnowledgeBaseWriteRefusal`, served as-is to API clients (the chat
#: upload's ``knowledge_base_skipped_reason``), so these strings are API.
KB_WRITE_PACKAGE_INSTALLED = "package_installed"
KB_WRITE_BUILTIN = "builtin"
KB_WRITE_COMPANY_MANAGED = "company_managed"
KB_WRITE_NOT_PERMITTED = "not_permitted"
KB_WRITE_READ_ONLY = "read_only"
KB_WRITE_WORKSPACE = "workspace"


@dataclass(frozen=True)
class KnowledgeBaseWriteRefusal:
    """A refused knowledge-base content write: a stable code and its sentence."""

    reason: str
    detail: str


@functools.cache
def _user_content_builtin_slugs() -> frozenset[str]:
    """Slugs of the built-in knowledge bases whose documents are the customer's.

    Their seed entry sets ``user_content``: the platform seeds the collection
    and its configuration, never its documents (Company Context), so a document
    added there is never reverted by a boot and stays writable.
    """
    from app.agents.builtin_agents import BUILTIN_KNOWLEDGE_BASES

    return frozenset(kb["slug"] for kb in BUILTIN_KNOWLEDGE_BASES if kb.get("user_content"))


def builtin_knowledge_base_holds_user_content(kb: KnowledgeBase) -> bool:
    """True for a built-in KB whose seed entry leaves its documents to the customer."""
    if not getattr(kb, "is_builtin", False):
        return False
    slugs = _user_content_builtin_slugs()
    return any(
        isinstance(tag, str) and tag.startswith("slug:") and tag[5:] in slugs
        for tag in (getattr(kb, "tags", None) or [])
    )


def knowledge_base_content_lock(kb: KnowledgeBase, *, package_installed: bool) -> str | None:
    """The lock on *kb*'s content, whoever writes - or ``None`` when unlocked.

    ``package_installed`` / ``workspace`` / ``builtin`` / ``read_only`` (the
    ``KB_WRITE_*`` codes). The one rule behind the content-write gate
    (:func:`knowledge_base_write_refusal`), the agents' write scopes
    (:func:`content_locked_knowledge_base_ids`) and the ``content_lock`` the
    API serves on a knowledge base, so the UI never offers a write grant the
    runtime would strip.
    """
    if package_installed:
        return KB_WRITE_PACKAGE_INSTALLED
    if owning_workspace_id(kb) is not None:
        return KB_WRITE_WORKSPACE
    if getattr(kb, "is_builtin", False) and not builtin_knowledge_base_holds_user_content(kb):
        return KB_WRITE_BUILTIN
    if getattr(kb, "is_read_only", False):
        return KB_WRITE_READ_ONLY
    return None


async def content_locked_knowledge_base_ids(
    db: AsyncSession, kbs: Sequence[KnowledgeBase]
) -> dict[str, str]:
    """``{kb_id: lock}`` for the content-locked KBs among *kbs* (loaded rows).

    One origin query for the whole list, never one per KB: callers strip these
    from an agent's write scope on the chat and assignment hot paths.
    """
    if not kbs:
        return {}
    from_packages = await package_origin_knowledge_base_ids(db, [kb.id for kb in kbs])
    locks: dict[str, str] = {}
    for kb in kbs:
        lock = knowledge_base_content_lock(
            kb, package_installed=str(kb.id) in from_packages
        )
        if lock is not None:
            locks[str(kb.id)] = lock
    return locks


def read_only_knowledge_base_error(
    kb: KnowledgeBase, *, action: str = "modify its documents"
) -> str | None:
    """Return the refusal for mutating a read-only knowledge base's content.

    A read-only KB is ground truth: it stays searchable and readable, only its
    *content* is frozen - document add / upload / crawl / one-shot import /
    update / delete, and deleting the collection. Re-embedding and configured
    import sources are not content mutations and are never refused here.
    Unlocking is a config change anyone who manages the KB may make, which is
    why :func:`knowledge_base_write_refusal` checks this lock last.
    """
    if getattr(kb, "is_read_only", False):
        return (
            f"'{kb.name}' is read-only: its content is locked, so you cannot "
            f"{action}. Turn off read-only in the knowledge base "
            "configuration first."
        )
    return None


def manage_knowledge_base_error(
    user: User,
    kb: KnowledgeBase,
    user_group_ids: list[str] | None,
    *,
    action: str = "modify",
) -> str | None:
    """Return the refusal when *user* may not manage *kb*, else ``None``.

    Company-managed KBs are admin-only; a personal KB is managed by its
    owner, an admin or a ``config``-tier grantee (:func:`can_manage_as`).
    """
    is_admin = bool(getattr(user, "is_admin", False))
    if kb.is_company_managed and not is_admin:
        return f"Only admins can {action} company-managed knowledge bases"
    if not can_manage_as(user.id, is_admin, kb, user_group_ids):
        return f"You don't have permission to {action} this knowledge base."
    return None


async def knowledge_base_write_refusal(
    db: AsyncSession,
    kb: KnowledgeBase,
    user: User | None = None,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify the documents of",
    content_action: str = "modify its documents",
    participle: str = "modified",
) -> KnowledgeBaseWriteRefusal | None:
    """The one gate every write to a knowledge base's CONTENT runs.

    Shared by the REST document / crawl / import / delete routes, the
    agent-builder document tools, ``store_knowledge`` /
    ``update_knowledge_document`` and the chat upload, so they refuse the
    same KBs for the same reasons (the ``writable_agent_error()`` model).
    In order:

    1. Installed by a skill package - the package re-materializes it.
       Owned by a workspace - written by whoever manages the
       workspace, never by an agent's grant, and its internal Conversations
       knowledge base by the indexer alone; nothing below applies to it.
    2. Built-in - seeded and re-synced at startup, immutable for everyone,
       except a built-in KB whose seed entry leaves its documents to the
       customer (``user_content``, Company Context): its documents follow
       the rules below like any company-managed KB.
    3. When *user* is given, the caller must manage the KB
       (:func:`manage_knowledge_base_error`). ``None`` means an agent writing
       under the write grant its configuration holds, which is that grant's
       own authorization.
    4. Read-only - liftable by whoever manages the KB, hence after 3.

    ``action`` / ``content_action`` / ``participle`` phrase the refusals
    (``"add documents to"`` / ``"add documents to it"`` / ``"modified"``).
    """
    origin_err = await package_origin_knowledge_base_error(
        db, kb.id, action=participle
    )
    lock = knowledge_base_content_lock(kb, package_installed=origin_err is not None)
    if origin_err is not None:
        return KnowledgeBaseWriteRefusal(KB_WRITE_PACKAGE_INSTALLED, origin_err)
    if lock == KB_WRITE_WORKSPACE:
        # A person who manages the workspace writes its content; an agent's
        # write grant never reaches it - such a knowledge base is bound to no
        # agent, and a stale scope must not open it.
        if user is None:
            return KnowledgeBaseWriteRefusal(
                KB_WRITE_WORKSPACE, await workspace_managed_knowledge_base_error(db, kb) or ""
            )
        if kb.is_internal:
            # The Conversations knowledge base: only the indexer writes it.
            return KnowledgeBaseWriteRefusal(
                KB_WRITE_WORKSPACE, f"A workspace's conversations cannot be {participle} by hand."
            )
        if user_group_ids is None and not getattr(user, "is_admin", False):
            user_group_ids = await get_user_group_ids(db, user.id)
        if not await can_manage_entity(db, user, kb, user_group_ids):
            return KnowledgeBaseWriteRefusal(
                KB_WRITE_NOT_PERMITTED,
                f"You don't have permission to {action} this knowledge base.",
            )
        return None
    if lock == KB_WRITE_BUILTIN:
        return KnowledgeBaseWriteRefusal(
            KB_WRITE_BUILTIN, f"Built-in knowledge bases cannot be {participle}"
        )
    if user is not None:
        is_admin = bool(getattr(user, "is_admin", False))
        if user_group_ids is None and not is_admin and not kb.is_company_managed:
            user_group_ids = await get_user_group_ids(db, user.id)
        if manage_err := manage_knowledge_base_error(
            user, kb, user_group_ids, action=action
        ):
            reason = (
                KB_WRITE_COMPANY_MANAGED
                if kb.is_company_managed and not is_admin
                else KB_WRITE_NOT_PERMITTED
            )
            return KnowledgeBaseWriteRefusal(reason, manage_err)
    if lock == KB_WRITE_READ_ONLY and (
        read_only_err := read_only_knowledge_base_error(kb, action=content_action)
    ):
        return KnowledgeBaseWriteRefusal(KB_WRITE_READ_ONLY, read_only_err)
    return None


async def writable_knowledge_base_error(
    db: AsyncSession,
    kb: KnowledgeBase,
    user: User | None = None,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify the documents of",
    content_action: str = "modify its documents",
    participle: str = "modified",
) -> str | None:
    """:func:`knowledge_base_write_refusal` as the refusal sentence, else ``None``."""
    refusal = await knowledge_base_write_refusal(
        db,
        kb,
        user,
        user_group_ids,
        action=action,
        content_action=content_action,
        participle=participle,
    )
    return refusal.detail if refusal is not None else None


async def assert_writable_knowledge_base(
    db: AsyncSession,
    kb: KnowledgeBase,
    user: User,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify the documents of",
    content_action: str = "modify its documents",
    participle: str = "modified",
) -> None:
    """Raise 403 when *user* may not write *kb*'s content.

    Thin HTTPException wrapper around :func:`knowledge_base_write_refusal`.
    """
    err = await writable_knowledge_base_error(
        db,
        kb,
        user,
        user_group_ids,
        action=action,
        content_action=content_action,
        participle=participle,
    )
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


SHARE_PERMISSION_READ_ONLY = "read_only"
SHARE_PERMISSION_CONFIG = "config"
SHARE_PERMISSION_CONFIG_WITH_CHATS = "config_with_chats"
SHARE_PERMISSIONS_CONFIG = {
    SHARE_PERMISSION_CONFIG,
    SHARE_PERMISSION_CONFIG_WITH_CHATS,
}

#: Permission ordering, shared by the group and user resolvers so the two can
#: be compared against each other in :func:`get_effective_permission`.
_SHARE_PERMISSION_RANK = {
    SHARE_PERMISSION_READ_ONLY: 0,
    SHARE_PERMISSION_CONFIG: 1,
    SHARE_PERMISSION_CONFIG_WITH_CHATS: 2,
}

#: Every tier a share grant may carry, in ascending order of power.
SHARE_PERMISSIONS_ALL = (
    SHARE_PERMISSION_READ_ONLY,
    SHARE_PERMISSION_CONFIG,
    SHARE_PERMISSION_CONFIG_WITH_CHATS,
)


async def normalize_user_grants(
    db: AsyncSession,
    shared_user_ids: list[str] | None,
    shared_user_permissions: dict[str, str] | None,
    *,
    existing_ids: list[str] | None = None,
    owner_id: uuid.UUID | str | None = None,
) -> tuple[list[str] | None, dict[str, str] | None]:
    """Validate and canonicalise per-user share grants before storage.

    Returns the ``(ids, permissions)`` pair to persist — ``(None, None)`` when
    the entity ends up with no user grants, matching how the group columns
    store "not shared" as NULL rather than ``[]``.

    Three things are enforced here rather than at each of the eleven call
    sites:

    * **Canonical ids.** ``shared_user_ids`` is compared in SQL by
      ``_shared_user_ids_contains``, which cannot normalise what it reads, so
      a non-canonical id would store a grant that never matches at query time
      — a share that silently does nothing.
    * **Real users.** These are JSON arrays with no foreign key, so an id for
      a user who does not exist would sit in the sharing UI as a ghost row
      forever. Unknown ids are rejected rather than dropped: silently
      discarding a grant the caller asked for is the kind of failure an
      access-control write should never do quietly.

    *existing_ids* are the grants already stored on the entity, and they are
    **grandfathered past the active-account check**. Only a *newly added* id
    has to name an active user. Without this, deactivating someone would 422
    every subsequent edit of every entity shared with them — a rename, an
    ``enabled`` toggle, anything — because the stored grant list is revalidated
    on each write. Deactivation is a reversible toggle, so the grant has to
    survive it; an id whose account was *deleted* is still dropped, since
    there is nothing left to reactivate.
    * **Known tiers.** An unrecognised permission string would rank as ``0``
      in :func:`_best_permission` and silently behave as ``read_only``.
    * **Locked accounts.** Every account the grants name is read ``FOR KEY
      SHARE``, the lock a foreign key check takes, and held until the write
      commits. :func:`prune_user_grants` locks the account before it reads any
      grant, so a write racing a deletion either commits first and is pruned,
      or waits and finds the account gone. Read without flushing, so the row
      being edited is not locked before the accounts, the order the deletion
      takes them in.

    Permission entries naming a user who is not in *shared_user_ids* are
    dropped — they grant nothing, and keeping them would resurrect a stale
    tier if that user were re-added later.
    """
    if not shared_user_ids:
        return None, None

    # The owner reaches their own entity unconditionally, so a grant naming
    # them confers nothing. Dropped rather than rejected: the share picker
    # already excludes self, so this only shows up on an imported or cloned
    # entity that carried a grant to whoever now owns the copy, and 422-ing
    # that would break import for a difference that changes no access.
    owner = _canonical_uuid(owner_id)

    canonical: list[str] = []
    for raw in shared_user_ids:
        cid = _canonical_uuid(raw)
        if cid is None:
            raise HTTPException(
                status_code=422, detail=f"Invalid user id in share grants: {raw!r}"
            )
        if cid == owner:
            continue
        if cid not in canonical:  # de-duplicate, preserving caller order
            canonical.append(cid)

    if not canonical:
        return None, None

    result = await db.execute(
        select(cast(User.id, SAString), User.is_active)
        .where(User.id.in_([uuid.UUID(cid) for cid in canonical]))
        .with_for_update(read=True, key_share=True)
        .execution_options(autoflush=False)
    )
    rows = {str(uuid.UUID(row[0])): bool(row[1]) for row in result.all()}
    grandfathered = {
        cid
        for cid in (_canonical_uuid(raw) for raw in (existing_ids or []))
        if cid is not None
    }

    # A grant that is already stored only has to still name a real account;
    # a newly added one has to name an *active* account too.
    rejected = [
        cid
        for cid in canonical
        if cid not in grandfathered and not rows.get(cid, False)
    ]
    if rejected:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot share with unknown or inactive user(s): "
                + ", ".join(rejected)
            ),
        )
    known = {cid for cid in canonical if cid in rows}
    canonical = [cid for cid in canonical if cid in known]
    if not canonical:
        return None, None

    permissions: dict[str, str] = {}
    for raw_key, level in (shared_user_permissions or {}).items():
        key = _canonical_uuid(raw_key)
        if key is None or key not in known:
            continue
        if level not in SHARE_PERMISSIONS_ALL:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid share permission {level!r}. "
                    f"Expected one of: {', '.join(SHARE_PERMISSIONS_ALL)}"
                ),
            )
        permissions[key] = level

    return canonical, permissions or None


async def normalize_group_grants(
    db: AsyncSession,
    shared_group_ids: list[str] | None,
    shared_group_permissions: dict[str, str] | None,
    *,
    existing_ids: list[str] | None = None,
) -> tuple[list[str] | None, dict[str, str] | None]:
    """Validate and canonicalise group share grants before storage.

    The group counterpart of :func:`normalize_user_grants`, and the one writer
    of ``shared_group_ids`` / ``shared_group_permissions`` on a shareable
    entity (:func:`apply_group_share_fields` for a request body). Returns the
    ``(ids, permissions)`` pair to persist, ``(None, None)`` for no grant.

    * **Canonical ids**, de-duplicated in caller order: the resolvers compare
      the stored strings with the canonical ids of the caller's groups, so a
      non-canonical id would store a grant that never matches. An id that is
      not a UUID is refused (422).
    * **Real groups.** A newly added id has to name an existing group (422
      otherwise). An id in *existing_ids* (the grants already stored) whose
      group was deleted since is dropped, so a leftover never blocks the next
      edit.
    * **Locked groups.** Every group the grants name is read ``FOR KEY
      SHARE`` until the write commits. :func:`prune_group_grants` locks the
      group ``FOR UPDATE`` before it reads any grant, so a write racing a
      deletion either commits first and is pruned, or waits and finds the
      group gone. Read without flushing, so the edited row is not locked
      before the groups, the order the deletion takes them in.
    * **Known tiers**, and permission entries only for granted groups: an
      unknown tier would rank as ``read_only``, a stray entry would resurrect
      a stale tier if the group were granted again.
    """
    if not shared_group_ids:
        return None, None

    canonical: list[str] = []
    for raw in shared_group_ids:
        cid = _canonical_uuid(raw)
        if cid is None:
            raise HTTPException(
                status_code=422, detail="Invalid group id in share grants."
            )
        if cid not in canonical:
            canonical.append(cid)

    result = await db.execute(
        select(cast(Group.id, SAString))
        .where(Group.id.in_([uuid.UUID(cid) for cid in canonical]))
        .with_for_update(read=True, key_share=True)
        .execution_options(autoflush=False)
    )
    known = {str(uuid.UUID(row[0])) for row in result.all()}
    stored = {
        cid
        for cid in (_canonical_uuid(raw) for raw in (existing_ids or []))
        if cid is not None
    }

    rejected = [cid for cid in canonical if cid not in known and cid not in stored]
    if rejected:
        raise HTTPException(
            status_code=422,
            detail="Cannot share with unknown group(s): " + ", ".join(rejected),
        )
    canonical = [cid for cid in canonical if cid in known]
    if not canonical:
        return None, None

    permissions: dict[str, str] = {}
    for raw_key, level in (shared_group_permissions or {}).items():
        key = _canonical_uuid(raw_key)
        if key is None or key not in known:
            continue
        if level not in SHARE_PERMISSIONS_ALL:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Invalid share permission. "
                    f"Expected one of: {', '.join(SHARE_PERMISSIONS_ALL)}"
                ),
            )
        permissions[key] = level

    return canonical, permissions or None


#: Models whose sharing columns copy another entity's grants instead of holding
#: grants of their own. A flow run records who could read its flow when it was
#: triggered: like the frozen graph next to it, the copy is history. It is never
#: shown, and a deleted user or group in it resolves to no one, so the deletion
#: cleanups leave it alone rather than scan a table that grows with every
#: trigger and is never trimmed.
GRANT_SNAPSHOT_MODELS: tuple[type, ...] = (FlowRun,)


def share_grant_models(
    column: Literal["shared_user_ids", "shared_group_ids"],
) -> tuple[type, ...]:
    """Every mapped model holding grants in *column*, in table-name order.

    Derived from the mappers, as :func:`reassign_company_managed_entities` is,
    so a new shareable model is pruned when a user or a group is deleted
    without a list to keep in step. :data:`GRANT_SNAPSHOT_MODELS` are left out.
    """
    import app.models  # noqa: F401 - every model registered
    from app.core.database import Base

    return tuple(
        sorted(
            (
                mapper.class_
                for mapper in Base.registry.mappers
                if column in mapper.columns
                and mapper.class_ not in GRANT_SNAPSHOT_MODELS
            ),
            key=lambda model_cls: model_cls.__table__.name,
        )
    )


def _locked_for_pruning(model_cls: type, condition: Any):
    """*model_cls* rows matching *condition*, locked and re-read as committed.

    A grant edit that commits between this read and the prune's flush would
    otherwise be written over with the list read before it. Id order, so two
    deletions pruning the same rows lock them in the same order.
    """
    return (
        select(model_cls)
        .where(condition)
        .order_by(model_cls.id)
        .with_for_update(of=model_cls)
        .execution_options(populate_existing=True)
    )


async def prune_user_grants(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Remove every per-user share grant naming *user_id*. Returns rows changed.

    ``shared_user_ids`` is a JSON array with no foreign key, so a deleted user
    is not cascaded out of it the way ``ConversationShare`` rows are. The
    leftover id grants nothing — UUIDs are never reused and the account can no
    longer authenticate — but it would sit in the sharing UI forever as a
    grant to a person who does not exist. Call this before the user row is
    deleted, on every model :func:`share_grant_models` returns.

    The account row is locked first, with the ``FOR UPDATE`` its ``DELETE``
    takes anyway, against the ``FOR KEY SHARE`` :func:`normalize_user_grants`
    takes on every account a grant write names: a share edit racing the
    deletion either commits before the grants are read, or waits and finds the
    account gone.

    Deliberately a read-modify-write in Python rather than a ``jsonb`` update
    expression: the ids are compared canonicalised, as the resolvers compare
    them, both columns are handled the same way on every model, and this runs
    once per user deletion. An LLM provider's audience has ids and no
    permission map.
    """
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    target = str(user_id)
    changed = 0

    for model_cls in share_grant_models("shared_user_ids"):
        result = await db.execute(
            _locked_for_pruning(model_cls, _shared_user_ids_contains(model_cls, user_id))
        )
        for entity in result.scalars().all():
            remaining = [
                raw
                for raw in (entity.shared_user_ids or [])
                if _canonical_uuid(raw) != target
            ]
            entity.shared_user_ids = remaining or None
            if hasattr(model_cls, "shared_user_permissions"):
                perms = {
                    key: value
                    for key, value in (entity.shared_user_permissions or {}).items()
                    if _canonical_uuid(key) != target
                }
                entity.shared_user_permissions = perms or None
            changed += 1

    from app.llm.provider_access import prune_user_exclusions

    changed += await prune_user_exclusions(db, user_id)

    if changed:
        logger.info(
            "Pruned per-user share grants for deleted user %s across %d entities",
            target,
            changed,
        )
    return changed


async def prune_group_grants(db: AsyncSession, group_id: uuid.UUID) -> int:
    """Remove every group share grant naming *group_id*. Returns rows changed.

    The group counterpart of :func:`prune_user_grants`, on every model
    :func:`share_grant_models` returns: call it before the group row is
    deleted. The group row is locked first, against the ``FOR KEY SHARE`` an
    LLM provider access write takes on the groups it adds
    (``provider_access.apply_provider_access``). Group grants are matched
    exactly, as :func:`get_group_permission` matches them; the text
    ``contains`` only narrows which rows are loaded. LLMProviderConfig has
    group ids and no permission map. The members' agents then stop naming the
    pruned agents they no longer reach
    (``agent_bindings.unlink_lost_holders_of``).
    """
    from app.models.agent import Agent
    from app.services.agent_bindings import unlink_lost_holders_of

    await db.execute(select(Group.id).where(Group.id == group_id).with_for_update())
    target = str(group_id)
    changed = 0
    pruned_agents: list[Agent] = []

    for model_cls in share_grant_models("shared_group_ids"):
        result = await db.execute(
            _locked_for_pruning(
                model_cls,
                and_(
                    model_cls.shared_group_ids.isnot(None),
                    cast(model_cls.shared_group_ids, SAText).contains(target),
                ),
            )
        )
        for entity in result.scalars().all():
            ids = entity.shared_group_ids
            if not isinstance(ids, list) or target not in ids:
                continue
            entity.shared_group_ids = [gid for gid in ids if gid != target] or None
            perms = getattr(entity, "shared_group_permissions", None)
            if isinstance(perms, dict) and target in perms:
                entity.shared_group_permissions = {
                    key: value for key, value in perms.items() if key != target
                } or None
            if isinstance(entity, Agent):
                pruned_agents.append(entity)
            changed += 1
    await unlink_lost_holders_of(db, pruned_agents)
    return changed


async def apply_group_share_fields(
    db: AsyncSession,
    entity: Any,
    body: Any,
    *,
    partial: bool,
    null_keeps_stored: bool = False,
) -> None:
    """Validate and copy ``shared_group_ids`` / ``shared_group_permissions``
    from *body* onto *entity* through :func:`normalize_group_grants`.

    The group counterpart of :func:`apply_user_share_fields`, called where a
    route used to copy the two fields. *partial* selects update semantics (a
    field absent from ``model_fields_set`` keeps the stored value) versus
    create semantics (both fields are authoritative). *null_keeps_stored* is
    for the update routes whose contract reads an explicit ``null`` as "keep
    the stored value" too. The pair is normalised together whenever the
    request changes either field, so a permission map is checked against the
    ids it ends up with; a request changing neither leaves the stored grants
    alone.
    """
    fields_set: set[str] = (
        {
            name
            for name in getattr(body, "model_fields_set", set())
            if not (null_keeps_stored and getattr(body, name, None) is None)
        }
        if partial
        else set()
    )
    if partial and not ({"shared_group_ids", "shared_group_permissions"} & fields_set):
        return

    def _incoming(name: str) -> Any:
        if partial and name not in fields_set:
            return getattr(entity, name, None)
        return getattr(body, name, None)

    entity.shared_group_ids, entity.shared_group_permissions = (
        await normalize_group_grants(
            db,
            _incoming("shared_group_ids"),
            _incoming("shared_group_permissions"),
            # Grants already stored stay unless their group was deleted; only
            # a newly added id must name an existing group.
            existing_ids=getattr(entity, "shared_group_ids", None),
        )
    )


def wants_access_restriction(body: Any, entity: CompanyManagedEntity) -> bool:
    """Return the ``access_restricted`` value *body* will end up storing.

    Needed *before* the grants are applied, because the company-managed toggle
    has to decide whether to clear them — see ``handle_company_managed_toggle``'s
    ``keep_grants``. A request that does not mention the field keeps whatever
    the entity already carries.
    """
    if "access_restricted" in getattr(body, "model_fields_set", set()):
        return bool(getattr(body, "access_restricted", None))
    return bool(getattr(entity, "access_restricted", False))


async def apply_user_share_fields(
    db: AsyncSession,
    entity: CompanyManagedEntity,
    body: Any,
    *,
    partial: bool,
) -> None:
    """Validate and copy the sharing fields from *body* onto *entity*.

    Call this **after** ``is_company_managed`` has been applied to *entity*,
    since ``access_restricted`` is only coherent relative to it.

    *partial* selects update semantics (``PATCH``-style: a field absent from
    ``model_fields_set`` leaves the stored value alone) versus create
    semantics (every field is authoritative).

    When the request touches either share field, the ids and the permission
    map are normalised **as a pair**: a request that changes just
    ``shared_user_permissions`` still has to be validated against the ids
    already stored, or it could leave a tier pointing at a user who is no
    longer granted anything. When it touches neither, normalisation is skipped
    outright — an unrelated edit must not depend on the stored grants still
    being revalidatable.
    """
    fields_set: set[str] = (
        getattr(body, "model_fields_set", set()) if partial else set()
    )

    def _incoming(name: str, current: Any) -> Any:
        if partial and name not in fields_set:
            return current
        return getattr(body, name, None)

    # Skip the round-trip entirely when this request says nothing about
    # sharing. Revalidating the stored grants on every unrelated edit buys
    # nothing and costs a query per write.
    touches_grants = (not partial) or bool(
        {"shared_user_ids", "shared_user_permissions"} & fields_set
    )
    if touches_grants:
        stored_ids = getattr(entity, "shared_user_ids", None)
        entity.shared_user_ids, entity.shared_user_permissions = (
            await normalize_user_grants(
                db,
                _incoming("shared_user_ids", stored_ids),
                _incoming(
                    "shared_user_permissions",
                    getattr(entity, "shared_user_permissions", None),
                ),
                # Grants already on the entity survive their grantee being
                # deactivated; only newly added ones must name an active user.
                existing_ids=stored_ids,
                owner_id=getattr(entity, "user_id", None),
            )
        )

    if not entity.is_company_managed:
        # Clearing rather than rejecting: an entity that stops being
        # company-managed has no blanket visibility left to withdraw, and
        # 422-ing that transition would block an unrelated edit. A caller who
        # explicitly asks for the contradiction still gets told.
        explicitly_set = (not partial) or ("access_restricted" in fields_set)
        if explicitly_set:
            validate_access_restricted(
                access_restricted=getattr(body, "access_restricted", None),
                is_company_managed=False,
            )
        entity.access_restricted = False
        return

    entity.access_restricted = bool(
        _incoming("access_restricted", getattr(entity, "access_restricted", False))
    )
    if not entity.access_restricted:
        # Company-managed and unrestricted: every user can already see it, so
        # both grant lists are dead weight. Clearing keeps the sharing UI
        # honest — a grant row that confers nothing reads as though it does —
        # and matches what the company-managed toggle has always done to the
        # group grants.
        entity.shared_user_ids = None
        entity.shared_user_permissions = None
        entity.shared_group_ids = None
        entity.shared_group_permissions = None


def validate_access_restricted(
    *,
    access_restricted: bool | None,
    is_company_managed: bool,
) -> None:
    """Reject ``access_restricted`` on an entity that is not company-managed.

    The flag withdraws the blanket "visible to everyone" clause that
    ``is_company_managed`` grants. On a personal entity there is no such
    clause, so storing it would record an intent the system never acts on —
    a user would reasonably read "access restricted" as a guarantee. Failing
    the write makes the contradiction visible instead.
    """
    if access_restricted and not is_company_managed:
        raise HTTPException(
            status_code=422,
            detail=(
                "access_restricted applies only to company-managed entities. "
                "A personal entity is already limited to its explicit grants."
            ),
        )


def _canonical_uuid(value: Any) -> str | None:
    """Return *value* as a canonical UUID string, or ``None`` if unparseable.

    Grants are stored as JSON strings, so a hand-edited row (or an id written
    by an older client without hyphens) would silently fail a raw ``==``
    against the canonical form SQLAlchemy hands back for a UUID column.
    Normalising both sides keeps the Python-side resolvers agreeing with the
    SQL predicate, which compares canonical strings written by the API.
    """
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _best_permission(
    granted_ids: Sequence[str],
    permissions: dict,
    candidate_ids: Sequence[str],
) -> str | None:
    """Return the highest permission *candidate_ids* hold in *granted_ids*.

    ``None`` when none of the candidates appear in the grant list. A grant with
    no explicit entry in *permissions* counts as ``read_only`` — the tier the
    UI creates by default.
    """
    granted = set(granted_ids)
    best: str | None = None
    for cid in candidate_ids:
        if cid in granted:
            level = permissions.get(cid, SHARE_PERMISSION_READ_ONLY)
            if best is None or _SHARE_PERMISSION_RANK.get(
                level, 0
            ) > _SHARE_PERMISSION_RANK.get(best, 0):
                best = level
    return best


def get_group_permission(
    entity: CompanyManagedEntity,
    user_group_ids: list[str] | None,
) -> str | None:
    """Return the highest permission level the user has via group sharing.

    Returns ``None`` when the user has no group-based access.
    Priority: config_with_chats > config > read_only.
    """
    if not user_group_ids:
        return None
    return _best_permission(
        getattr(entity, "shared_group_ids", None) or [],
        getattr(entity, "shared_group_permissions", None) or {},
        user_group_ids,
    )


def get_user_permission(
    entity: CompanyManagedEntity,
    user_id: uuid.UUID | str | None,
) -> str | None:
    """Return the permission level the user has via a direct per-user grant.

    The per-person counterpart of :func:`get_group_permission`.
    Returns ``None`` when no grant names this user.
    """
    canonical = _canonical_uuid(user_id)
    if canonical is None:
        return None
    granted = [
        cid
        for cid in (
            _canonical_uuid(raw)
            for raw in (getattr(entity, "shared_user_ids", None) or [])
        )
        if cid is not None
    ]
    # Permission keys are normalised the same way, so a non-canonical key
    # cannot silently downgrade a grant to the read_only default.
    raw_perms: dict = getattr(entity, "shared_user_permissions", None) or {}
    perms = {
        key: value
        for key, value in (
            (_canonical_uuid(k), v) for k, v in raw_perms.items()
        )
        if key is not None
    }
    return _best_permission(granted, perms, [canonical])


def get_effective_permission(
    entity: CompanyManagedEntity,
    user_id: uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
) -> str | None:
    """Return the highest permission across the user's direct and group grants.

    A person can be reached by both a personal grant and a group they belong
    to; the stronger of the two wins, so adding someone to a group can only
    ever widen their access, never silently narrow a grant they already had.
    """
    candidates = [
        get_user_permission(entity, user_id),
        get_group_permission(entity, user_group_ids),
    ]
    best: str | None = None
    for perm in candidates:
        if perm is None:
            continue
        if best is None or _SHARE_PERMISSION_RANK.get(
            perm, 0
        ) > _SHARE_PERMISSION_RANK.get(best, 0):
            best = perm
    return best


def can_manage_as(
    user_id: uuid.UUID | str | None,
    is_admin: bool,
    entity: CompanyManagedEntity,
    user_group_ids: list[str] | None = None,
) -> bool:
    """:func:`can_manage` for a caller known by id and role only.

    The one manage rule, for code paths that carry ``user_id`` + ``is_admin``
    rather than a ``User`` row (the tool builders, which run as the acting
    user): a company-managed entity is managed by admins only; a personal one
    by its creator, an admin, or a ``config`` / ``config_with_chats`` grantee.
    An agent of the Platform Health fleet is managed by nobody, admins included
    (``services.health.read_only``): the seed rewrites it on every boot.
    """
    from app.services.health.read_only import is_health_agent

    if is_health_agent(entity):
        return False
    if entity.is_company_managed:
        return is_admin
    if is_admin or (user_id is not None and entity.user_id == user_id):
        return True
    perm = get_effective_permission(entity, user_id, user_group_ids)
    return perm is not None and perm in SHARE_PERMISSIONS_CONFIG


def can_manage(
    user: User,
    entity: CompanyManagedEntity,
    user_group_ids: list[str] | None = None,
) -> bool:
    """Return True if the user may edit/delete the entity.

    Company-managed entities can only be managed by admins.
    Personal entities can be managed by the creator, an admin, or anyone
    holding a ``config`` / ``config_with_chats`` grant — whether that grant
    names them directly or reaches them through a group. ``read_only``
    grantees can see but NOT edit.

    Entity responses carry this, evaluated for the requesting user, as
    ``can_manage``: the UI gates its edit surfaces on that flag instead of
    re-deriving share tiers in the browser.
    """
    return can_manage_as(user.id, user.role == "admin", entity, user_group_ids)


def can_authorize_integration(user: User, integration: Integration) -> bool:
    """Return True if an OAuth authorization by *user* can land on *integration*.

    The OAuth callback (``oauth_callbacks._handle_integration_callback``)
    stores the token only on an integration owned by the user who started the
    flow, or on a company-managed one, whose credential then moves to that user -
    and ``start_oauth`` keeps a company-managed integration to
    admins. So: company-managed, an admin;
    personal, its owner and nobody else (a ``config`` grantee or an admin on
    someone else's personal integration would end on the callback's 404). The
    token is the owner's own, the same rule as for using it.

    ``start_oauth`` enforces it and integration responses carry it as
    ``can_authorize``.
    """
    if integration.is_company_managed:
        return user.role == "admin"
    return integration.user_id is not None and integration.user_id == user.id


def can_view_agent_chats(
    user: User,
    agent: Agent,
    user_group_ids: list[str] | None = None,
) -> bool:
    """Return True if *user* may read *agent*'s conversations.

    Narrower than :func:`can_manage`: an admin or the agent's owner, and
    otherwise only a ``config_with_chats`` grantee of a personal agent -
    whether the grant names them directly or reaches them through a group.
    The chats routes enforce it, and agent responses carry it as
    ``can_view_chats`` so the UI never offers a tab the API refuses.
    """
    if user.role == "admin" or (agent.user_id is not None and agent.user_id == user.id):
        return True
    if agent.is_company_managed:
        return False
    return (
        get_effective_permission(agent, user.id, user_group_ids)
        == SHARE_PERMISSION_CONFIG_WITH_CHATS
    )


def webhook_secret_visible(
    user: User,
    assignment: Assignment,
    agent: Agent | None,
    user_group_ids: list[str] | None = None,
) -> bool:
    """Return True if *user* may see the assignment's ``webhook_secret``.

    The secret is a delivery credential, so visibility tracks the *manage*
    contract rather than the (wider) *view* contract:

    - admins always see it;
    - company-managed assignments are manageable only by admins, so every
      other viewer gets it redacted;
    - the owner sees it (they configure the sending system);
    - viewers reached by a share of the parent *agent* (group or per-user)
      see it only with ``config``-tier permission — ``read_only`` grantees
      can view the assignment but must not receive its credential (mirrors
      :func:`can_manage`).

    Single source of truth shared by the REST API
    (``assignments._visible_trigger_config``) and the agent-builder
    ``get_assignment`` tool so the two surfaces can never drift apart.
    When *agent* is ``None`` (parent row unavailable) the group-permission
    tier cannot be evaluated and the check fails closed for non-owners.
    """
    if user.is_admin:
        return True
    if assignment.is_company_managed:
        return False
    if assignment.user_id == user.id:
        return True
    if agent is not None:
        return can_manage(user, agent, user_group_ids)
    return False


def run_visible_filter(user_id: uuid.UUID):
    """Return a SQLAlchemy filter clause for ``AssignmentRun`` rows a user may see.

    Run visibility is **own + company-managed only** and is intentionally
    NARROWER than agent / assignment visibility: a group-shared agent is
    read-only-visible to every group member (they can browse its config and
    assignments), but its *runs* stay private to the run owner. Company-managed
    runs are visible to all users (read-only).

    This is the single source of truth for "which runs can this user see",
    shared by every surface that lists, counts, or summarises runs — the runs
    list / count, notifications, dashboard digests / failed runs / KPIs, the
    run-detail / descendants endpoints, etc. Keeping them all routed through
    one predicate prevents the drift that previously let a group-shared agent's
    runs leak into the dashboard and notifications while still 404-ing on open
    (the run-detail endpoint already used this exact scope).

    Admins skip it where they open one run or one entity's runs: the run
    detail, an assignment's runs, and the run feeds read with ``scope=all``
    (an agent's or an assignment's Activity tab). The feeds without it stay on
    this predicate for everyone, like every library list.

    The company-managed tier leaves out, for a non-administrator, the runs of
    an agent whose activity is hidden (``hide_activity_from_non_admins`` on
    the agent or its home fleet, #11624); with every flag off it is unchanged.
    """
    from app.services.activity_visibility import agent_activity_shown

    return or_(
        AssignmentRun.user_id == user_id,
        and_(
            AssignmentRun.is_company_managed.is_(True),
            agent_activity_shown(AssignmentRun.agent_id, user_id),
        ),
    )


def fleet_filter(agent_column, fleet: str):
    """The ``?fleet=`` filter of the agent and schedule reads.

    A fleet id keeps its home members, ``none`` the agents in no fleet (the
    playground); anything else is a 422 naming the parameter. The clause is
    :func:`app.services.fleet_views.fleet_scope_clause`, shared with the
    fleet views.
    """
    from app.services.fleet_views import FLEET_FILTER_NONE, fleet_scope_clause

    if fleet != FLEET_FILTER_NONE:
        fleet = str(parse_uuid(fleet, label="fleet"))
    return fleet_scope_clause(agent_column, fleet)


def workspace_filter(workspace: str, workspace_column, *agent_columns):
    """The ``?workspace=`` filter of the Operations lists.

    Keeps what is filed in the workspace or produced by the fleet it hosts
    (:func:`app.services.fleet_views.workspace_scope_clause`); an id that is
    not a UUID is a 422 naming the parameter.
    """
    from app.services.fleet_views import workspace_scope_clause

    return workspace_scope_clause(parse_uuid(workspace, label="workspace"), workspace_column, *agent_columns)


def fleet_operations_filter(fleet: str, *agent_columns):
    """The ``?fleet=`` filter of the Operations lists.

    Keeps the rows one of *agent_columns* (an owner, a creator, the parent or
    the target of a delegation) names as a current home member of the fleet,
    orchestrator included and connected agents left out: the population of
    :func:`app.services.fleet_views.fleet_scope_clause`, resolved when the
    query runs. Only a fleet id is accepted here (no ``none``); anything else is
    a 422 naming the parameter. It narrows the caller's visible rows, never
    widens them.
    """
    from app.services.fleet_views import fleet_scope_clause

    fleet_id = parse_uuid(fleet, label="fleet")
    clauses = [fleet_scope_clause(column, fleet_id) for column in agent_columns]
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def combine_scopes(*clauses):
    """The AND of the scope clauses that are set, or None when none is."""
    present = [clause for clause in clauses if clause is not None]
    if not present:
        return None
    return present[0] if len(present) == 1 else and_(*present)


def run_answerable_filter(user: User):
    """Rows whose pending approvals *user* may actually decide.

    Deliberately NARROWER than :func:`run_visible_filter`, and the difference
    matters: visibility shows every company-managed run to every user, while
    :func:`require_run_access` lets only an administrator answer one. A query
    built on the visible filter would offer a reviewer runs it would then
    refuse one by one.

    This is the set form of that same rule, kept beside it so the two cannot
    drift — every caller that answers approvals in bulk must select with this
    and still authorise each row with ``require_run_access``, which stays the
    single point of truth for one row.
    """
    if getattr(user, "role", None) == "admin":
        return or_(
            AssignmentRun.user_id == user.id,
            AssignmentRun.is_company_managed.is_(True),
        )
    return and_(
        AssignmentRun.user_id == user.id,
        AssignmentRun.is_company_managed.is_(False),
    )


# The keys of a delegation run's ``trigger_payload`` that name its parent run.
PARENT_RUN_PAYLOAD_KEYS = frozenset({"parent_run_id", "parent_agent_id"})


async def openable_parent_runs(
    session: AsyncSession,
    parent_run_ids: set[uuid.UUID],
    user_id: uuid.UUID,
    *,
    is_admin: bool,
) -> dict[uuid.UUID, uuid.UUID]:
    """``{run id: agent id}`` of the *parent_run_ids* the caller may open.

    A run names its parent - id, agent, the payload keys that carry them - only
    to a caller who could open that parent, the rule the descendants list
    follows: every run for an admin, ``run_visible_filter`` otherwise.
    """
    if not parent_run_ids:
        return {}
    query = select(AssignmentRun.id, AssignmentRun.agent_id).where(
        AssignmentRun.id.in_(parent_run_ids)
    )
    if not is_admin:
        query = query.where(run_visible_filter(user_id))
    return {run_id: agent_id for run_id, agent_id in (await session.execute(query)).all()}


def parent_is_shown(run: AssignmentRun, openable, *, is_admin: bool) -> bool:
    """Whether a reader is told *run*'s parent: always by an admin, and a run
    whose parent row is gone keeps what it says; otherwise only a parent in
    *openable* (the ids :func:`openable_parent_runs` returned) is named."""
    return is_admin or not run.parent_run_id or run.parent_run_id in openable


def without_hidden_parent(payload, parent_shown: bool):
    """*payload* without the keys naming a parent run the reader cannot open."""
    if parent_shown or not isinstance(payload, dict):
        return payload
    if PARENT_RUN_PAYLOAD_KEYS.isdisjoint(payload):
        return payload
    return {k: v for k, v in payload.items() if k not in PARENT_RUN_PAYLOAD_KEYS}


def require_run_access(
    run: AssignmentRun,
    user: User,
    *,
    action: str = "modify",
) -> None:
    """Raise 403/404 if *user* may not interact with *run*.

    Admins act on every run, like the run detail they open it from.
    Company-managed runs (``is_company_managed``) are otherwise refused;
    personal runs require the owning user.
    """
    if user.role == "admin":
        return
    if run.is_company_managed:
        raise HTTPException(
            status_code=403,
            detail=f"Only admins can {action} company-managed assignment runs",
        )
    if run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")


def continuing_run_is_company_managed(
    source_is_company_managed: bool | None,
    *,
    fallback: bool,
    ceiling: bool,
) -> bool:
    """The ``is_company_managed`` of a run that continues another run.

    One rule for every run that continues another - a delegation child
    (sub-agent task, consult, transfer), a follow-up, a chained run, a retry -
    because it carries that run's input or result: it is never more visible
    than the run it continues. It takes that run's flag, and never more
    than *ceiling*, the widest its own settings allow: for a run of an
    assignment, the assignment's visibility and ``hide_runs`` as they are now;
    ``True`` (no cap of its own) for a delegation child or a retry, which have
    no settings of their own. *ceiling* has no default: every caller states
    its cap. *fallback* stands in for the source's flag when it is unknown (no
    source run, or its row could not be read). Its owner is the other half
    (:func:`can_open_continued_run`).
    """
    flag = fallback if source_is_company_managed is None else bool(source_is_company_managed)
    return flag and ceiling


CONTINUED_RUN_NOT_OPENABLE_CODE = "continued_run_not_openable"


class ContinuedRunNotOpenableError(HTTPException):
    """A run would run as a user who cannot open the run it continues:
    403 ``continued_run_not_openable``. An ``HTTPException``
    so a request path answers the reason rather than a 500; the chain
    dispatch reports it to the assignment's owner
    (``_report_refused_continuation``)."""

    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            detail={
                "code": CONTINUED_RUN_NOT_OPENABLE_CODE,
                "message": "The user this run would run as cannot open the run it continues, whose result it would carry.",
            },
        )


async def can_open_continued_run(
    session: AsyncSession,
    source: AssignmentRun,
    user_id: uuid.UUID | None,
) -> bool:
    """Whether a run acting as *user_id* may continue *source*.

    *user_id* is the identity the continuation acts as: its stored user, else
    its agent's owner, as the engine resolves it
    (``assignment_engine._acting_identity``). A run that continues another
    carries that run's id and its input or result, so that identity must be
    able to open it: the rule a run names its parent by
    (:func:`openable_parent_runs`). The other half of
    :func:`continuing_run_is_company_managed`: a private run is read by its
    owner, a company-managed one by everyone. A missing or deactivated user
    opens nothing (authentication refuses them), the run's own user included,
    and no identity at all is no verifiable principal.
    """
    if user_id is None:
        return False
    identity = (await session.execute(select(User.role, User.is_active).where(User.id == user_id))).one_or_none()
    if identity is None or not identity.is_active:
        return False
    if user_id == source.user_id:
        return True
    return source.id in await openable_parent_runs(session, {source.id}, user_id, is_admin=identity.role == "admin")


async def resolve_child_run_is_company_managed(
    session: AsyncSession,
    parent_run_id: str | uuid.UUID | None,
    *,
    fallback: bool,
) -> bool:
    """Resolve the ``is_company_managed`` flag for a child ``AssignmentRun``.

    Delegation/handover children (sub-agent background tasks, consult, and
    transfer runs) must mirror the shared-visibility of their parent run. The
    always-set-owner invariant means a company-managed run now carries an
    owner/run-as ``user_id``, so deriving the child's visibility from
    ``user_id`` / anonymity alone would mark children of a shared run tree as
    private (``is_company_managed=False``) and hide them from shared
    run/status views.

    Reads the parent run's ``is_company_managed`` and applies
    :func:`continuing_run_is_company_managed` to it. Falls back to *fallback*
    (the caller's legacy ownerless signal) only when there is no
    ``parent_run_id`` or the parent row cannot be loaded — never worse than the
    pre-fix behaviour.
    """
    if not parent_run_id:
        return continuing_run_is_company_managed(None, fallback=fallback, ceiling=True)
    try:
        run_uuid = (
            parent_run_id
            if isinstance(parent_run_id, uuid.UUID)
            else uuid.UUID(str(parent_run_id))
        )
        flag = (
            await session.execute(
                select(AssignmentRun.is_company_managed).where(
                    AssignmentRun.id == run_uuid
                )
            )
        ).scalar_one_or_none()
    except Exception:
        flag = None
    return continuing_run_is_company_managed(flag, fallback=fallback, ceiling=True)


async def reassign_company_managed_entities(
    session: AsyncSession,
    from_user_id: uuid.UUID,
    to_user_id: uuid.UUID,
) -> dict[str, int]:
    """Reassign every company-managed row owned by *from_user_id* to *to_user_id*.

    Company-managed entities now preserve a real ``user_id`` owner (the
    always-set-owner invariant) instead of nulling it. Their
    ``users.id`` foreign key is ``ON DELETE CASCADE``, so deleting that owner
    would cascade-delete shared platform resources the whole company depends on
    — agents (and, transitively, their assignments and runs), integrations,
    knowledge bases, MCP servers, custom tools, channel bots, prompts,
    variables, and any future company-managed entity.

    Call this immediately before deleting a user so their company-managed rows
    are handed to a surviving admin and outlive the owner. Private rows are
    intentionally left untouched: they CASCADE-delete with the user because they
    are personal to them.

    Every mapped table that exposes both an ``is_company_managed`` and a
    ``user_id`` column is reassigned, so a newly added company-managed entity is
    covered automatically (no per-table maintenance, no silent data loss).

    Returns a ``{table_name: rows_reassigned}`` map; tables with no reassigned
    rows are omitted.
    """
    import app.models  # noqa: F401 — ensure every model is registered
    from app.core.database import Base

    reassigned: dict[str, int] = {}
    seen: set[str] = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None or table.name in seen:
            continue
        cols = table.c
        if "is_company_managed" not in cols or "user_id" not in cols:
            continue
        seen.add(table.name)
        result = await session.execute(
            table.update()
            .where(cols.user_id == from_user_id)
            .where(cols.is_company_managed.is_(True))
            .values(user_id=to_user_id)
        )
        rowcount = getattr(result, "rowcount", 0) or 0
        if rowcount:
            reassigned[table.name] = rowcount
    return reassigned


async def resolve_company_managed_owner_id(
    session: AsyncSession,
) -> uuid.UUID | None:
    """Resolve the owner to stamp on a company-managed entity.

    Single source of truth for the always-set-owner invariant:
    company-managed rows keep a real ``user_id`` owner (the admin) and rely on
    ``is_company_managed`` for visibility instead of nulling the owner. Used by
    every code path that creates a company-managed row without an authenticated
    user to attribute it to: the startup seed pass (which also backfills the
    owner on legacy ownerless rows) and platform registration.

    Resolution order: the configured ``ADMIN_EMAIL`` user → the oldest admin →
    the oldest user → ``None``. ``None`` is only returned on a brand-new DB
    before any user row exists; the next startup seed/sync pass then backfills
    the owner once the admin user is provisioned.
    """
    from app.core.config import settings

    row = await session.execute(
        select(User.id)
        .where(func.lower(User.email) == settings.admin_email)
        .order_by(User.created_at)
        .limit(1)
    )
    owner = row.scalar_one_or_none()
    if owner:
        return owner

    row = await session.execute(
        select(User.id).where(User.role == "admin").order_by(User.created_at).limit(1)
    )
    owner = row.scalar_one_or_none()
    if owner:
        return owner

    row = await session.execute(select(User.id).order_by(User.created_at).limit(1))
    return row.scalar_one_or_none()


async def apply_owner_transfer(
    db: AsyncSession,
    *,
    actor: User,
    entity: Any,
    new_owner_id: str | None,
    entity_type: str,
    entity_name: str,
    request: Request | None = None,
) -> bool:
    """Admin-only owner (``user_id``) transfer for an ownable entity.

    Reassigns ``entity.user_id`` to *new_owner_id* and records a dedicated
    ``transfer_ownership`` audit entry. Shared by every ownable entity's update
    endpoint (agents, integrations, knowledge bases, MCP servers, custom tools,
    prompts, agent skills) so the admin-only contract and validation can never
    drift between them.

    A no-op returning ``False`` when *new_owner_id* is ``None`` or already the
    current owner. Returns ``True`` when the owner was reassigned (so callers
    can cascade — e.g. agents mirror the owner onto their assignments).

    Raises :class:`HTTPException`:
      * 403 when *actor* is not an admin and attempts a real transfer — checked
        BEFORE UUID validation, so a non-admin never sees a 422 (which would
        leak that the owner field is processed for them),
      * 422 when an admin supplies a *new_owner_id* that is not a valid UUID,
      * 404 when the target user does not exist,
      * 400 when the target user is deactivated.
    """
    if new_owner_id is None:
        return False
    # Parse the target id, but defer a malformed-UUID 422 until AFTER the admin
    # gate below: a non-admin attempting any transfer must get a 403, not a 422
    # that reveals the owner field was even processed for them. A parse failure
    # can never equal the current owner, so it is never a benign no-op.
    try:
        new_uuid: uuid.UUID | None = parse_uuid(new_owner_id, label="owner user_id")
    except HTTPException as exc:
        new_uuid, parse_error = None, exc
    else:
        parse_error = None
    # Unchanged owner = benign no-op for anyone (the frontend never sends
    # ``user_id`` for non-admins, but re-saving the current owner must not 403).
    if new_uuid is not None and entity.user_id == new_uuid:
        return False
    if not actor.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only admins can change the owner of an entity",
        )
    if parse_error is not None:
        # Admin supplied a malformed UUID — surface the precise 422.
        raise parse_error
    target = await db.get(User, new_uuid)
    if target is None:
        raise HTTPException(status_code=404, detail="Target owner user not found")
    if not target.is_active:
        raise HTTPException(
            status_code=400,
            detail="Cannot transfer ownership to a deactivated user",
        )

    old_owner_id = entity.user_id
    entity.user_id = new_uuid

    # Local import keeps helpers free of an import cycle with the audit service.
    from app.services.audit import log_audit

    await log_audit(
        db,
        user=actor,
        action="transfer_ownership",
        entity_type=entity_type,
        entity_id=getattr(entity, "id", None),
        entity_name=entity_name,
        summary=(
            f"Transferred ownership of {entity_type} '{entity_name}' "
            f"to {target.full_name or target.email}"
        ),
        details={
            "old_owner_id": str(old_owner_id) if old_owner_id else None,
            "new_owner_id": str(new_uuid),
            "new_owner_email": target.email,
        },
        request=request,
    )
    return True


async def get_or_create_intent(
    session: AsyncSession,
    name: str,
    description: str | None = None,
):
    """Race-safe get-or-create for an :class:`Intent` row by unique ``name``.

    A plain check-then-insert crashes the loser with a
    ``UniqueViolationError`` on ``ix_intents_name`` when two writers race —
    the startup seed pass on a booting worker versus another worker's seed
    pass or a live pod handling platform registration.
    ``INSERT ... ON CONFLICT (name) DO NOTHING`` + re-select is atomic: the
    losing insert blocks until the winner commits, inserts nothing, and the
    follow-up SELECT (READ COMMITTED) returns the winner's row.

    The ``created`` flag comes from the insert's rowcount (``INSERT 0 0``
    when the ON CONFLICT skipped), so a loser that raced a concurrent
    creator correctly reports ``False`` instead of claiming the row.

    The row is flushed/inserted but NOT committed — the caller owns the
    transaction boundary, like the other helpers in this module.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.intent import Intent

    result = await session.execute(select(Intent).where(Intent.name == name))
    intent = result.scalar_one_or_none()
    if intent is not None:
        return intent, False

    insert_result = await session.execute(
        pg_insert(Intent)
        .values(name=name, description=description)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    # Normalize rowcount (None / -1 on drivers that can't report it) the
    # same way as the other rowcount consumers in this codebase, so an
    # indeterminate count never reports created=True.
    created = (getattr(insert_result, "rowcount", 0) or 0) > 0
    result = await session.execute(select(Intent).where(Intent.name == name))
    return result.scalar_one(), created


async def get_or_create_vertical(
    session: AsyncSession,
    name: str,
    *,
    source: str = "manual",
    platform_identifier: str | None = None,
):
    """Race-safe get-or-create for a :class:`Vertical` row by unique ``name``.

    Same ``ON CONFLICT DO NOTHING`` + re-select shape as
    :func:`get_or_create_intent` — ``verticals.name`` is unique and is
    written concurrently by the startup seed pass and platform registration.
    The ``created`` flag comes from the insert's rowcount so
    a loser that raced a concurrent creator reports ``False``. Caller owns
    the transaction boundary.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.intent import Vertical

    result = await session.execute(select(Vertical).where(Vertical.name == name))
    vertical = result.scalar_one_or_none()
    if vertical is not None:
        return vertical, False

    insert_result = await session.execute(
        pg_insert(Vertical)
        .values(name=name, source=source, platform_identifier=platform_identifier)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    # Same rowcount normalization as get_or_create_intent.
    created = (getattr(insert_result, "rowcount", 0) or 0) > 0
    result = await session.execute(select(Vertical).where(Vertical.name == name))
    return result.scalar_one(), created


def writable_agent_error(
    agent: Agent,
    user: User,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify",
    allow_builtin: bool = False,
) -> str | None:
    """Return a user-facing error string when *user* may not mutate *agent*.

    Used by both the API layer (wrapped in ``HTTPException``) and the agent
    builder tool layer (returned directly to the LLM as a tool result) so
    the same three gates always apply consistently:

    1. Built-in agents are read-only in XTM One mode (admins included).
    2. Company-managed agents are writable only by admins.
    3. Group-shared agents require ``config``-tier permission.

    Returns ``None`` when the user is allowed to mutate the agent — which
    covers writing the agent itself and any of its assignments.

    ``action`` is rendered into the message in active present-tense voice
    (``"Cannot <action> ..."``, ``"Only admins can <action> ..."``) so it
    accepts any verb or verb phrase — ``"edit"``, ``"delete"``,
    ``"add an assignment to"`` — without producing malformed English. The
    earlier ``"Built-in agents cannot be {action}d."`` phrasing required
    ``action`` to be a verb that becomes a past participle by appending
    ``d`` (so ``"edit"`` → ``"editd"`` and ``"add an assignment to"`` →
    ``"add an assignment tod"``); the new wording avoids that landmine.

    ``allow_builtin`` drops gate 1 ONLY, leaving 2 and 3 exactly as they are —
    so a built-in, being company-managed, still resolves to administrator-only.

    It exists for the one class of change that rule was never meant to catch:
    a built-in's **connected integrations**. Gate 1 protects the agent's seeded
    definition — persona, name, tools, model — which ``seed._sync_agent_fields``
    rewrites on every restart, so an edit there would silently drift back.
    Integration bindings are not part of that definition; they are the
    deployment's own accounts, and which ones a built-in may use is a decision
    only an administrator can make. A2A exposure is one of them, since binding
    an A2A integration IS the opt-in. Callers must pass this only after
    confirming the change is confined to those fields — see
    ``agents._is_integration_binding_change``, which is that confirmation.
    """
    from app.core.config import settings
    from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_agent

    # The Platform Health fleet is read-only in every mode, whatever the
    # carve-out (``allow_builtin``) and whoever asks.
    if is_health_agent(agent):
        return HEALTH_READ_ONLY_REFUSAL
    if agent.is_builtin and settings.is_xtm_one and not allow_builtin:
        return f"Cannot {action} built-in agents."
    if not can_manage(user, agent, user_group_ids):
        if agent.is_company_managed:
            return f"Only admins can {action} company-managed agents."
        return f"You don't have permission to {action} this agent."
    return None


def assert_writable_agent(
    agent: Agent,
    user: User,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify",
) -> None:
    """Raise 403 if *user* may not mutate *agent* (or its assignments).

    Thin HTTPException wrapper around :func:`writable_agent_error` so API
    routes can keep their existing ``raise HTTPException`` style while
    tools and other non-API call sites reuse the same gate logic.
    """
    err = writable_agent_error(agent, user, user_group_ids, action=action)
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


def writable_assignment_error(
    assignment: Assignment,
    agent: Agent,
    user: User,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify",
    allow_builtin: bool = False,
) -> str | None:
    """Return a user-facing error string when *user* may not mutate *assignment*.

    Mirrors :func:`writable_agent_error` for assignments — the gates are:

    1. The parent agent must not be built-in in XTM One mode.
    2. Company-managed assignments (``is_company_managed``) require admin role.
    3. Otherwise, the user must be able to manage the parent agent (own,
       admin, or group ``config``-tier sharing).

    Same active-voice ``"Cannot <action> ..."`` phrasing as
    :func:`writable_agent_error` so any verb / verb phrase renders
    cleanly without the previous ``"{action}d"`` past-participle landmine.

    ``allow_builtin`` drops gate 1 only, as it does for
    :func:`writable_agent_error`: for a change that leaves the seeded
    definition of a built-in alone (purging an assignment's runs).
    """
    from app.core.config import settings
    from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_agent

    if is_health_agent(agent):
        return HEALTH_READ_ONLY_REFUSAL
    if agent.is_builtin and settings.is_xtm_one and not allow_builtin:
        return f"Cannot {action} assignments on built-in agents."
    if assignment.is_company_managed and not user.is_admin:
        return f"Only admins can {action} company-managed assignments."
    if not can_manage(user, agent, user_group_ids):
        return f"You don't have permission to {action} assignments on this agent."
    return None


def assert_writable_assignment(
    assignment: Assignment,
    agent: Agent,
    user: User,
    user_group_ids: list[str] | None = None,
    *,
    action: str = "modify",
    allow_builtin: bool = False,
) -> None:
    """Raise 403 if *user* may not mutate *assignment*.

    Thin HTTPException wrapper around :func:`writable_assignment_error`.
    """
    err = writable_assignment_error(
        assignment,
        agent,
        user,
        user_group_ids,
        action=action,
        allow_builtin=allow_builtin,
    )
    if err is not None:
        raise HTTPException(status_code=403, detail=err)


# ── Deadlock-safe agent deletion ─────────────────────────────────────────────
#
# Deleting an agent cascades across child tables that reference ``agents`` with
# TWO foreign keys each — ``agent_delegations`` and ``sub_agent_tasks`` carry
# ``parent_agent_id`` + ``target_agent_id`` (ON DELETE CASCADE), and
# ``conversations`` / ``work_items`` carry three nullable agent FKs each (ON
# DELETE SET NULL). When two agents that reference each other are deleted
# concurrently, the two DELETEs cascade onto the SAME child rows in opposite
# lock order and PostgreSQL aborts one of them with a 40P01 deadlock. This was
# observed in production when the agent-builder chat tool emitted several
# ``delete_agent`` calls in parallel (``asyncio.gather``), each on its own
# session.
#
# Two defenses, combined, make agent deletion ALWAYS succeed:
#
#   1. ``acquire_agent_delete_lock`` takes a transaction-scoped advisory lock on
#      a single global key, serializing EVERY agent delete across all backend
#      instances so two agent DELETEs can never interleave their cascades.
#      Acquired before any row lock (a single global lock order => no deadlock
#      among deletes) and released automatically on COMMIT/ROLLBACK.
#   2. ``run_with_deadlock_retry`` is a bounded retry for the residual deadlock /
#      serialization class (an agent delete racing a concurrent NON-delete
#      writer, e.g. a chat inserting a conversation that references the agent).
#
# Stable bigint key for the transaction-scoped advisory lock that serializes
# agent deletes (``b"agtdel"`` big-endian -> fits int8). Mirrors the mcp_oauth
# DCR-lock convention: a derived constant bound as a parameter (not interpolated
# into the SQL) with an explicit bigint cast that pins the single-key
# ``pg_advisory_xact_lock(int8)`` overload. That single-key space is SEPARATE
# from the two-key ``(int4, int4)`` advisory locks taken elsewhere (e.g. the
# sub-agent fan-out cap in ``services.sub_agent``) — PostgreSQL records the two
# forms under different lock tags — so the two can never collide.
_AGENT_DELETE_LOCK_KEY = int.from_bytes(b"agtdel", "big")
_AGENT_DELETE_MAX_RETRIES = 6
_AGENT_DELETE_RETRY_BASE_DELAY = 0.05  # seconds (exponential backoff + jitter)
_ACQUIRE_AGENT_DELETE_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(CAST(:lock_key AS bigint))"
)


def is_retryable_db_error(exc: BaseException) -> bool:
    """Return ``True`` for transient Postgres deadlock / serialization errors.

    Matches the asyncpg SQLSTATEs (``40P01`` deadlock_detected, ``40001``
    serialization_failure) and a textual fallback, mirroring
    ``services.kb_sync._is_retryable_embedding_delete_error`` so the whole
    codebase classifies retryable DB contention the same way.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if sqlstate in {"40P01", "40001"}:
        return True
    msg = str(exc).lower()
    return "deadlock" in msg or "serialization" in msg


async def acquire_agent_delete_lock(db: AsyncSession) -> None:
    """Serialize agent deletions on a transaction-scoped advisory lock.

    Acquire this lock before any row-locking DML (the DELETE itself and the
    reference purge) so every agent delete takes its row locks in one global
    order and concurrent deletes can never interleave their cascades into a
    deadlock. Harmless preceding reads (e.g. the permission/visibility SELECT in
    the agent-builder delete path) are fine - a plain read takes no row locks.
    The lock auto-releases on COMMIT/ROLLBACK.
    """
    await db.execute(
        _ACQUIRE_AGENT_DELETE_LOCK_SQL, {"lock_key": _AGENT_DELETE_LOCK_KEY}
    )


async def run_with_deadlock_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int = _AGENT_DELETE_MAX_RETRIES,
    base_delay: float = _AGENT_DELETE_RETRY_BASE_DELAY,
    label: str = "operation",
) -> T:
    """Run *operation*, retrying transient deadlock / serialization failures.

    *operation* must be self-contained and safe to re-run: it owns its
    transaction (begin -> work -> commit) and rolls back on failure, because
    each retry re-invokes it from scratch (typically opening a fresh session).
    Non-retryable errors propagate immediately; the last error is re-raised once
    retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable_db_error(exc):
                raise
            # Full jitter exponential backoff; operational, not cryptographic.
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(  # noqa: S311
                0, base_delay
            )
            logger.warning(
                "Retryable DB error during %s (attempt %d/%d), "
                "retrying in %.3fs: %s",
                label,
                attempt,
                max_retries,
                delay,
                format_exception_message(exc),
            )
            await asyncio.sleep(delay)
    # Only reachable if called with max_retries < 1 (the loop body otherwise
    # always returns or raises). Surface a clear error rather than None.
    raise RuntimeError(
        f"run_with_deadlock_retry({label}) exhausted without running the operation"
    )


def _prune_ctem_agent_overrides(
    stage_map: dict | None, brief_map: dict | None, aid: str
) -> tuple[dict | None, dict | None, bool]:
    """Remove agent id *aid* from the CTEM analyst override maps.

    ``stage_map`` is the vertical-aware ``{vertical: {stage: id}}`` map (or the
    legacy flat ``{stage: id}`` shape); ``brief_map`` is ``{vertical: id}``.
    Returns ``(new_stage, new_brief, changed)`` with emptied containers collapsed
    to ``None`` so a fully-cleared override reverts to the seeded default - the
    same "value disappears -> back to default" contract as the LLM-model cleanup.

    Ids are compared on their stripped form: the read/write paths trim override
    ids (``_vertical_stage_override_id`` / ``_apply_overrides``), so pruning must
    too, otherwise a stored id with stray whitespace would survive the delete.
    """
    changed = False
    new_stage = stage_map
    if isinstance(stage_map, dict):
        rebuilt: dict = {}
        for k, v in stage_map.items():
            if isinstance(v, dict):  # nested per-vertical bucket
                bucket = {s: i for s, i in v.items() if str(i).strip() != aid}
                if len(bucket) != len(v):
                    changed = True
                if bucket:
                    rebuilt[k] = bucket
            elif isinstance(v, str) and v.strip() == aid:  # legacy flat {stage: id}
                changed = True
            else:
                rebuilt[k] = v
        new_stage = rebuilt or None
    new_brief = brief_map
    if isinstance(brief_map, dict):
        rebuilt_b = {k: v for k, v in brief_map.items() if str(v).strip() != aid}
        if len(rebuilt_b) != len(brief_map):
            changed = True
        new_brief = rebuilt_b or None
    return new_stage, new_brief, changed


async def purge_agent_references(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Remove every reference to *agent_id* from agent-id columns that have NO
    database-level foreign key.

    Deleting an agent must never leave a dangling handover edge (a "ghost" node
    in the topology graph) or a stale default / linked / exposed-agent pointer.
    FK-backed references (assignments, runs, delegations, sub-agent tasks,
    memories, conversations, intents, ...) are removed by ON DELETE CASCADE /
    SET NULL and are intentionally NOT touched here.

    Covers the JSON / scalar agent-id columns that Postgres cannot clean for us:

      * agents.handover_agent_ids / handover_types / handover_conditions
      * agents.coordination_access (the agents it lists)
      * general_assistant_config.handover_agent_ids / handover_types
      * general_assistant_config.linked_agent_id
      * platform_settings.mcp_exposed_agent_ids
      * platform_settings.ctem_stage_agent_ids / ctem_briefing_agent_ids
      * users.default_agent_id / mcp_exposed_agent_ids / channel_agent_config
      * users.favorite_agent_ids
      * users.ctem_stage_agent_ids / ctem_briefing_agent_ids

    JSON lists/dicts keep their container type when emptied (``[]`` / ``{}``):
    for selection columns like ``mcp_exposed_agent_ids`` an empty list ("expose
    none") and NULL ("expose all") mean different things, so the container is
    preserved and only the scalar UUID pointers are nulled. A handover type map
    that held a type stays explicit when the deleted agent was its only typed
    target (``handover_types_after_removal``): the targets left keep running as
    delegate instead of each getting every handover type.

    Runs in the caller's transaction (flushes via the ORM unit of work; the
    caller commits) so it is atomic with the agent delete.
    """
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.models.platform_settings import PlatformSettings
    from app.services.handover_types import handover_types_after_removal

    aid = str(agent_id)

    # ── Other agents' handover references ─────────────────────────────────
    # Pre-filter with a text ``contains`` so only rows that actually mention the
    # id are loaded. This is only a narrowing pre-filter (it may occasionally
    # load an extra row): the exact-equality checks below are authoritative
    # before any row is mutated.
    agent_rows = (
        (
            await db.execute(
                select(Agent).where(
                    or_(
                        cast(Agent.handover_agent_ids, SAText).contains(aid),
                        cast(Agent.handover_types, SAText).contains(aid),
                        cast(Agent.handover_conditions, SAText).contains(aid),
                        cast(Agent.coordination_access, SAText).contains(aid),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    for ag in agent_rows:
        if ag.id == agent_id:
            # The agent being deleted; its own row is removed anyway.
            continue
        access = getattr(ag, "coordination_access", None)
        if isinstance(access, dict) and any(
            aid in {str(x) for x in (access.get(key) or [])}
            for key in ("read_agent_ids", "write_agent_ids")
        ):
            ag.coordination_access = {
                **access,
                **{
                    key: [x for x in (access.get(key) or []) if str(x) != aid]
                    for key in ("read_agent_ids", "write_agent_ids")
                },
            }
        if ag.handover_agent_ids and any(str(x) == aid for x in ag.handover_agent_ids):
            ag.handover_agent_ids = [
                x for x in ag.handover_agent_ids if str(x) != aid
            ]
        if ag.handover_types and aid in {str(k) for k in ag.handover_types}:
            ag.handover_types = handover_types_after_removal(
                ag.handover_agent_ids or [],
                ag.handover_types,
                {k: v for k, v in ag.handover_types.items() if str(k) != aid},
            )
        if ag.handover_conditions and aid in {str(k) for k in ag.handover_conditions}:
            ag.handover_conditions = {
                k: v for k, v in ag.handover_conditions.items() if str(k) != aid
            }

    # ── General Assistant config (singleton) ──────────────────────────────
    ga = (
        await db.execute(select(GeneralAssistantConfig).limit(1))
    ).scalar_one_or_none()
    if ga is not None:
        if ga.handover_agent_ids and any(str(x) == aid for x in ga.handover_agent_ids):
            ga.handover_agent_ids = [
                x for x in ga.handover_agent_ids if str(x) != aid
            ]
        if ga.handover_types and aid in {str(k) for k in ga.handover_types}:
            ga.handover_types = handover_types_after_removal(
                ga.handover_agent_ids or [],
                ga.handover_types,
                {k: v for k, v in ga.handover_types.items() if str(k) != aid},
            )
        if ga.linked_agent_id == agent_id:
            ga.linked_agent_id = None
            # Nobody chose "no agent": the platform default applies again.
            ga.linked_agent_configured = False

    # ── Platform settings (singleton) ─────────────────────────────────────
    ps = (await db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none()
    if (
        ps is not None
        and ps.mcp_exposed_agent_ids
        and any(str(x) == aid for x in ps.mcp_exposed_agent_ids)
    ):
        ps.mcp_exposed_agent_ids = [
            x for x in ps.mcp_exposed_agent_ids if str(x) != aid
        ]
    # CTEM analyst overrides (platform defaults): drop the deleted agent so the
    # stage / briefing reverts to its seeded default.
    if ps is not None:
        new_stage, new_brief, ctem_changed = _prune_ctem_agent_overrides(
            getattr(ps, "ctem_stage_agent_ids", None),
            getattr(ps, "ctem_briefing_agent_ids", None),
            aid,
        )
        if ctem_changed:
            ps.ctem_stage_agent_ids = new_stage
            ps.ctem_briefing_agent_ids = new_brief

    # ── Users (per-user agent pointers) ───────────────────────────────────
    # ``default_agent_id`` is a bare UUID column (no FK) — null it in one
    # statement for every user that pointed at the deleted agent.
    await db.execute(
        User.__table__.update()
        .where(User.default_agent_id == agent_id)
        .values(default_agent_id=None, default_agent_source=None, default_agent_set_at=None)
    )
    user_rows = (
        (
            await db.execute(
                select(User).where(
                    or_(
                        cast(User.mcp_exposed_agent_ids, SAText).contains(aid),
                        cast(User.channel_agent_config, SAText).contains(aid),
                        cast(User.favorite_agent_ids, SAText).contains(aid),
                        cast(User.ctem_stage_agent_ids, SAText).contains(aid),
                        cast(User.ctem_briefing_agent_ids, SAText).contains(aid),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    for u in user_rows:
        if u.mcp_exposed_agent_ids and any(
            str(x) == aid for x in u.mcp_exposed_agent_ids
        ):
            u.mcp_exposed_agent_ids = [
                x for x in u.mcp_exposed_agent_ids if str(x) != aid
            ]
        if u.channel_agent_config and any(
            str(v) == aid for v in u.channel_agent_config.values()
        ):
            u.channel_agent_config = {
                k: v for k, v in u.channel_agent_config.items() if str(v) != aid
            }
        # Favorites: drop the starred id so the agent list stops pinning a
        # ghost row. Emptied to ``[]`` rather than NULL — both read as "no
        # favorites", and keeping the container matches the columns above.
        if u.favorite_agent_ids and any(str(x) == aid for x in u.favorite_agent_ids):
            u.favorite_agent_ids = [
                x for x in u.favorite_agent_ids if str(x) != aid
            ]
        # CTEM analyst overrides (per-user): revert to the platform/seeded default.
        new_stage, new_brief, ctem_changed = _prune_ctem_agent_overrides(
            getattr(u, "ctem_stage_agent_ids", None),
            getattr(u, "ctem_briefing_agent_ids", None),
            aid,
        )
        if ctem_changed:
            u.ctem_stage_agent_ids = new_stage
            u.ctem_briefing_agent_ids = new_brief


async def get_user_agent(
    db: AsyncSession,
    user_id: uuid.UUID,
    agent_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> Agent:
    """Load an Agent visible to the user (own + group-shared + company-managed), or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    agent_uuid = parse_path_id(agent_id, not_found_detail="Agent not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(Agent).where(
            Agent.id == agent_uuid,
            user_visible_filter(Agent, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def get_user_assignment(
    db: AsyncSession,
    user_id: uuid.UUID,
    assignment_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> Assignment:
    """Load an Assignment whose parent agent is visible to the user, or raise 404.

    Visibility is :func:`assignment_visible_filter`. When *is_admin* is
    ``True``, visibility checks are bypassed. The HTTP wrapper of
    :func:`find_user_assignment`.
    """
    assignment_uuid = parse_path_id(assignment_id, not_found_detail="Assignment not found")
    assignment = await find_user_assignment(
        db, user_id, assignment_uuid, user_group_ids, is_admin=is_admin
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment


async def find_user_assignment(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    assignment_id: str | uuid.UUID,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
    lock: bool = False,
) -> Assignment | None:
    """Return the Assignment the user may reach by id, or ``None``.

    The ONE single-assignment lookup: the REST detail routes reach it through
    :func:`get_user_assignment`, the agent tools (agent builder, work-items
    delegation) call it directly. ``None`` covers an unknown id, a malformed
    one and an invisible one alike, so a caller answers all three the same
    way.
    *user_id* ``None`` is a caller with no account (the company-wide tier).

    An admin (*is_admin*) reaches every assignment, from the REST routes and
    from an agent tool alike: an admin opens and edits a colleague's agent and
    its assignments wherever they act.

    *lock* holds the row ``FOR UPDATE`` until the caller's commit, for a
    write validated against it.
    """
    assignment_uuid = assignment_id if isinstance(assignment_id, uuid.UUID) else safe_uuid(assignment_id)
    if assignment_uuid is None:
        return None
    if user_group_ids is None and not is_admin and user_id is not None:
        user_group_ids = await get_user_group_ids(db, user_id)
    stmt = select(Assignment).where(
        Assignment.id == assignment_uuid,
        assignment_visible_filter(user_id, user_group_ids, is_admin=is_admin),
    )
    if lock:
        stmt = stmt.with_for_update(of=Assignment)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class ActiveRun:
    """The run in flight an overlapping manual trigger was refused for."""

    id: str
    status: str
    created_at: datetime | None
    started_at: datetime | None

    @classmethod
    def of(cls, run: AssignmentRun | None) -> ActiveRun | None:
        if run is None:
            return None
        return cls(id=str(run.id), status=run.status, created_at=run.created_at, started_at=run.started_at)

    def describe(self) -> str:
        """What a tool tells its caller, e.g. "Run `9c7ed0d6-...` is running, started 2026-10-20 19:53:12 UTC."."""
        when = self.started_at or self.created_at
        verb = "started" if self.started_at else "created"
        stamp = f", {verb} {when.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC" if when else ""
        return f"Run `{self.id}` is {self.status}{stamp}."


@dataclass(frozen=True)
class ManualTriggerRefusal:
    """Why a manual trigger of a visible assignment is refused.

    ``status_code`` / ``detail`` are what ``POST /assignments/{id}/trigger``
    answers; ``reason`` lets a tool word the same refusal in its own terms.
    ``active_run`` is the run in flight an overlap refusal names, so a caller
    can wait on it instead of guessing.
    """

    status_code: int
    detail: str
    reason: str
    active_run: ActiveRun | None = None


def manual_trigger_refusal(
    assignment: Assignment,
    agent: Agent | None,
    *,
    is_admin: bool,
    action: str = "trigger",
) -> ManualTriggerRefusal | None:
    """The manual-trigger gate for an assignment the caller can already see.

    Shared by ``POST /assignments/{id}/trigger``, the agent-builder
    ``trigger_assignment`` tool and the work-items ``delegate_to_assignment``
    tool, which all start a run AS the caller. In order: the assignment and
    its agent must exist and be enabled; a company-managed assignment is
    admin-only unless it runs per user (``resolve_hide_runs``), in which case
    the run is the caller's own private report.
    """
    from app.services.assignment_helpers import resolve_hide_runs

    if not assignment.enabled:
        return ManualTriggerRefusal(400, "Assignment is disabled", "assignment_disabled")
    if agent is None:
        return ManualTriggerRefusal(404, "Agent not found", "agent_missing")
    if not agent.enabled:
        return ManualTriggerRefusal(400, "Agent is disabled", "agent_disabled")
    if (
        assignment.is_company_managed
        and not is_admin
        and not resolve_hide_runs(agent, assignment)
    ):
        return ManualTriggerRefusal(
            403, f"Only admins can {action} company-managed assignments", "admin_only"
        )
    return None


async def manual_trigger_overlap(
    db: AsyncSession,
    assignment: Assignment,
    agent: Agent,
    user_id: uuid.UUID,
) -> ManualTriggerRefusal | None:
    """The manual-trigger overlap guard: a run already in flight refuses.

    A per-user assignment (``resolve_hide_runs``) runs once per user, so only
    *user_id*'s own active run overlaps; any other assignment is guarded as a
    whole. Shared by the same three callers as :func:`manual_trigger_refusal`.
    The refusal names the run in flight (``active_run``).
    """
    return await active_run_refusal(
        db, assignment.id, manual_trigger_run_scope(assignment, agent, user_id)
    )


async def active_run_refusal(
    db: AsyncSession, assignment_id: uuid.UUID, run_scope: uuid.UUID | None
) -> ManualTriggerRefusal | None:
    """The overlap refusal for a run in flight in *run_scope*, naming it; ``None`` when there is none.

    *run_scope* is :func:`manual_trigger_run_scope`'s: the caller for a
    per-user assignment, ``None`` for the whole assignment.
    """
    from app.services.assignment_engine import find_active_run

    active = ActiveRun.of(await find_active_run(db, assignment_id, user_id=run_scope))
    if active is None:
        return None
    return manual_trigger_skipped(run_scope, active_run=active)


def manual_trigger_run_scope(
    assignment: Assignment, agent: Agent, user_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The ``active_run_user_id`` a manual trigger hands the engine.

    :func:`manual_trigger_overlap` is only a check; the three callers then
    call ``trigger_assignment(skip_if_active=True, active_run_user_id=...)``,
    whose advisory lock makes check and run creation atomic. The scope is the
    caller for a per-user assignment, the whole assignment otherwise.
    """
    from app.services.assignment_helpers import resolve_hide_runs

    return user_id if resolve_hide_runs(agent, assignment) else None


def manual_trigger_skipped(
    run_scope: uuid.UUID | None, *, active_run: ActiveRun | None = None
) -> ManualTriggerRefusal:
    """The refusal when the engine's atomic guard skipped the run: a
    concurrent trigger won the race :func:`manual_trigger_overlap` missed.
    A caller that can read the winning run passes it as *active_run*
    (:func:`active_run_refusal` reads it)."""
    if run_scope is not None:
        return ManualTriggerRefusal(
            409,
            "You already have a run in progress for this assignment",
            "own_run_active",
            active_run,
        )
    return ManualTriggerRefusal(409, "Assignment already has an active run", "run_active", active_run)


async def get_user_knowledge_base(
    db: AsyncSession,
    user_id: uuid.UUID,
    kb_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> KnowledgeBase:
    """Load a KnowledgeBase visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    kb_uuid = parse_path_id(kb_id, not_found_detail="Knowledge base not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb_uuid,
            user_visible_filter(
                KnowledgeBase, user_id, user_group_ids, is_admin=is_admin
            ),
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


async def get_user_custom_tool(
    db: AsyncSession,
    user_id: uuid.UUID,
    tool_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> CustomTool:
    """Load a CustomTool visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    tool_uuid = parse_path_id(tool_id, not_found_detail="Custom tool not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(CustomTool).where(
            CustomTool.id == tool_uuid,
            user_visible_filter(CustomTool, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Custom tool not found")
    return tool


async def get_user_mcp_server(
    db: AsyncSession,
    user_id: uuid.UUID,
    server_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> MCPServer:
    """Load an MCPServer visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    server_uuid = parse_path_id(server_id, not_found_detail="MCP server not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == server_uuid,
            user_visible_filter(MCPServer, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return srv


async def get_user_prompt(
    db: AsyncSession,
    user_id: uuid.UUID,
    prompt_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> Prompt:
    """Load a Prompt visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    prompt_uuid = parse_path_id(prompt_id, not_found_detail="Prompt not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(Prompt).where(
            Prompt.id == prompt_uuid,
            user_visible_filter(Prompt, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    prompt = result.scalar_one_or_none()
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt


from app.core.utils import format_exception_message
from app.services.kb_helpers import (
    enqueue_document_processing as enqueue_document_processing,
)
from app.services.kb_helpers import enqueue_documents as enqueue_documents
from app.services.kb_helpers import log_kb_activity as log_kb_activity

__all__ = ["enqueue_document_processing", "enqueue_documents", "log_kb_activity"]


async def get_user_integration(
    db: AsyncSession,
    user_id: uuid.UUID,
    integration_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
    lock: bool = False,
) -> Integration:
    """Load an Integration visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed. *lock* holds
    the row ``FOR UPDATE`` until the caller's commit and reads it afresh, for
    a write validated against what it stores.
    """
    integration_uuid = parse_path_id(integration_id, not_found_detail="Integration not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    query = select(Integration).where(
        Integration.id == integration_uuid,
        user_visible_filter(
            Integration, user_id, user_group_ids, is_admin=is_admin
        ),
    )
    if lock:
        query = query.with_for_update(of=Integration).execution_options(populate_existing=True)
    result = await db.execute(query)
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    return integration


async def integration_visible_filter(db: AsyncSession, user_id: Any):
    """:func:`user_visible_filter` on ``Integration`` for a user known by id only.

    Loads the user's groups and role, so a company-managed integration narrowed
    with ``access_restricted`` stays visible to its grantees and admins only;
    an admin sees every integration, as :func:`user_visible_filter`
    has it. With no (or an unparseable) user id: :func:`caller_visible_filter`
    for a caller with no account - an anonymous caller holds no grant.
    """
    uid = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(user_id)
    if uid is None:
        return caller_visible_filter(Integration, None)
    role = (
        await db.execute(select(User.role).where(User.id == uid))
    ).scalar_one_or_none()
    return user_visible_filter(
        Integration, uid, await get_user_group_ids(db, uid), is_admin=role == "admin"
    )


async def integration_visible_to_user(
    db: AsyncSession, integration_id: Any, user_id: Any
) -> bool:
    """Whether *user_id* may see the integration, per :func:`integration_visible_filter`.

    A value that is not an integration id names none: not visible, no query.
    """
    integration_uuid = (
        integration_id if isinstance(integration_id, uuid.UUID) else safe_uuid(integration_id)
    )
    if integration_uuid is None:
        return False
    row = (
        await db.execute(
            select(Integration.id).where(
                Integration.id == integration_uuid,
                await integration_visible_filter(db, user_id),
            )
        )
    ).first()
    return row is not None


async def get_user_channel_bot(
    db: AsyncSession,
    user_id: uuid.UUID,
    bot_id: str,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> ChannelBot:
    """Load a ChannelBot visible to the user, or raise 404.

    When *is_admin* is ``True``, visibility checks are bypassed.
    """
    bid = parse_path_id(bot_id, not_found_detail="Channel bot not found")
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(ChannelBot).where(
            ChannelBot.id == bid,
            user_visible_filter(ChannelBot, user_id, user_group_ids, is_admin=is_admin),
        )
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(status_code=404, detail="Channel bot not found")
    return bot


# ---------------------------------------------------------------------------
# Generic helpers to reduce duplication across CRUD endpoints
# ---------------------------------------------------------------------------


async def get_manageable_entity[T: CompanyManagedEntity](
    db: AsyncSession,
    model_cls: type[T],
    user: User,
    entity_id: str,
    *,
    action: str = "modify",
    entity_label: str = "Resource",
    user_group_ids: list[str] | None = None,
    lock: bool = False,
) -> T:
    """Load an entity by UUID, raising 404/403 as appropriate.

    Used directly by CRUD endpoints for company-managed entities
    (Custom Tools, MCP Servers, etc.) instead of per-module wrappers.

    A grantee needs a ``config`` / ``config_with_chats`` tier (see
    :func:`can_manage`); a ``read_only`` one gets the 403. An entity the
    caller cannot see (:func:`user_visible_filter`) gets the unknown-id 404,
    never the 403, which would confirm that it exists.

    *lock* holds the row ``FOR UPDATE`` until the caller's commit and reads it
    afresh, for a write validated against what it stores.
    """
    not_found = f"{entity_label} not found"
    entity_uuid = parse_path_id(entity_id, not_found_detail=not_found)
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    query = select(model_cls).where(model_cls.id == entity_uuid)
    if not user.is_admin:
        query = query.where(user_visible_filter(model_cls, user.id, user_group_ids))
    if lock:
        query = query.with_for_update(of=model_cls).execution_options(populate_existing=True)
    result = await db.execute(query)
    entity = result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail=not_found)
    if not can_manage(user, entity, user_group_ids):
        detail = (
            f"Only admins can {action} company-managed {entity_label.lower()}s"
            if entity.is_company_managed
            else f"Only the creator or an admin can {action} this {entity_label.lower()}"
        )
        raise HTTPException(status_code=403, detail=detail)
    return entity


#: The agent list fields a company-managed agent may only fill with
#: company-managed rows (``validate_company_managed_bindings``); custom tools,
#: skills and prompts stay allowed personal.
COMPANY_MANAGED_ONLY_AGENT_FIELDS = frozenset({"knowledge_base_ids", "mcp_server_ids"})


async def toggle_company_managed_list_field(
    db: AsyncSession,
    user: User,
    entity: CompanyManagedEntity,
    new_value: bool,
    *,
    agent_field: str | None,
) -> None:
    """Handle company-managed toggle for entities stored in agent list fields.

    Works for KBs (``knowledge_base_ids``), MCP servers (``mcp_server_ids``),
    and custom tools (``custom_tool_ids``).

    When switching company → private, removes the entity ID from other users'
    agent list fields so they lose access.

    ``agent_field=None`` is for entities agents never reference by id — e.g.
    ``TemplateContainer``, which agents discover through the
    ``search_templates`` tool's own visibility query rather than through a
    binding list. There is no stale binding to purge, so the guard-check +
    flag toggle + owner restore still run and the reference sweep is skipped.
    Callers must NOT hand-roll this path — the admin guard and the
    owner-restore fallback are the parts that must never drift.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only admins can change company-managed status"
        )

    entity.is_company_managed = new_value
    if new_value:
        # Private → company: keep user_id so original creator is preserved.
        # Visibility is controlled by is_company_managed flag, not user_id.
        pass
    else:
        # Company → private: restore to original creator (user_id preserved
        # from when the entity was made company-managed).  Fall back to the
        # current admin only for legacy entities that have no owner.
        if entity.user_id is None:
            entity.user_id = user.id
        if agent_field is None:
            # Nothing references this entity by id — no stale binding to purge.
            return
        owner_id = entity.user_id
        entity_id_str = str(entity.id)
        # Pre-filter with a text ``contains`` so only rows that actually
        # mention the id are loaded (same pattern as purge_agent_references)
        # instead of loading every other user's agents into memory. The
        # exact-membership check below stays authoritative.
        # Other owners' agents lose it. A company-managed agent loses it too,
        # its owner's included, for the bindings it may only hold
        # company-managed (#11866, ``validate_company_managed_bindings``).
        holders = and_(Agent.user_id != owner_id, Agent.user_id.isnot(None))
        if agent_field in COMPANY_MANAGED_ONLY_AGENT_FIELDS:
            holders = or_(holders, Agent.is_company_managed.is_(True))
        agents_result = await db.execute(
            select(Agent).where(
                holders,
                cast(getattr(Agent, agent_field), SAText).contains(entity_id_str),
            )
        )
        unbound_agents: list[Agent] = []
        for agent in agents_result.scalars().all():
            ids = getattr(agent, agent_field, None)
            if ids and entity_id_str in ids:
                setattr(
                    agent, agent_field, [eid for eid in ids if eid != entity_id_str]
                )
                unbound_agents.append(agent)

        # Losing a skill also loses the Custom Tools / KBs its package carried:
        # this is the same "skill detached from agent" event as an explicit
        # unbind, so it must not leave the derived bindings behind.
        # Imported locally to keep this shared helper module free of service
        # dependencies at import time.
        if agent_field == "skill_ids" and unbound_agents:
            from app.services.skill_package_bindings import (
                reconcile_agent_skill_package_bindings,
            )

            for agent in unbound_agents:
                await reconcile_agent_skill_package_bindings(db, agent)


async def handle_company_managed_toggle(
    db: AsyncSession,
    user: User,
    entity: CompanyManagedEntity,
    new_value: bool | None,
    *,
    agent_field: str | None,
    keep_grants: bool = False,
) -> None:
    """Handle company-managed toggle for list-field entities (KBs, MCP servers, etc.).

    Wraps the common guard-check → toggle → shared_group_ids cleanup pattern
    used by knowledge_bases, mcp_servers, custom_tools, agent_skills, and prompts.

    Pass ``agent_field=None`` for an entity agents never bind by id (see
    ``toggle_company_managed_list_field``) — the reference sweep is skipped, the
    guard-check and cleanup still run.

    Going company-managed normally clears the group grants, because "visible to
    everyone" subsumes "visible to group A". Pass ``keep_grants=True`` when the
    same request also sets ``access_restricted``: there the grants stop being
    redundant and *become* the audience, so clearing them would publish the
    entity platform-wide — the exact opposite of what was asked.

    No-ops when *new_value* is ``None`` or already matches the entity's current
    ``is_company_managed`` flag.
    """
    if new_value is None or new_value == entity.is_company_managed:
        return
    await toggle_company_managed_list_field(
        db,
        user,
        entity,
        new_value,
        agent_field=agent_field,
    )
    if new_value and not keep_grants:
        entity.shared_group_ids = None
        entity.shared_group_permissions = None


async def get_user_group_ids(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Return the list of group IDs (as strings) the user belongs to."""
    result = await db.execute(
        select(cast(GroupMember.group_id, SAString)).where(
            GroupMember.user_id == user_id
        )
    )
    return list(result.scalars().all())


def _shared_group_ids_overlap(
    model_cls: type[VisibilityQueryable], user_group_ids: list[str]
):
    """Check if ``shared_group_ids`` JSON array overlaps with the user's groups.

    Uses PostgreSQL JSONB ``?|`` (has-any) with a single ``text[]`` bind,
    so the database can evaluate all group IDs in one predicate and pick the
    matching GIN expression index on ``shared_group_ids::jsonb`` directly.

    The right-hand side is bound as ``text[]`` (not ``varchar[]``) so the
    operator signature matches PostgreSQL's ``jsonb ?| text[]`` definition
    exactly — implicit ``varchar[] → text[]`` casts can suppress GIN index
    usage on some plans, so an explicit ``text[]`` is the safer shape.

    ``shared_group_ids`` is stored as the request sent it, so on a table
    without that index one row holding a lone surrogate escape would abort
    the cast for every caller of the whole table: such a row's group
    grants read as none. The index refuses that value at write, and the bare
    predicate keeps it usable.
    """
    from sqlalchemy import literal_column

    if not user_group_ids:
        return literal_column("FALSE")

    jsonb_col = model_cls.shared_group_ids.cast(JSONB)
    overlap = jsonb_col.op("?|")(cast(user_group_ids, ARRAY(SAText)))
    if jsonb_cast_indexed(model_cls.shared_group_ids):
        return overlap
    return when_json_parseable(model_cls.shared_group_ids, overlap)


def _shared_user_ids_contains(
    model_cls: type[VisibilityQueryable], user_id: uuid.UUID
):
    """Check whether ``shared_user_ids`` names *user_id*.

    The per-person counterpart of :func:`_shared_group_ids_overlap`, using the
    same ``jsonb ?| text[]`` shape (with a one-element array) rather than the
    single-key ``?`` operator, so both predicates hit the same class of GIN
    expression index — ``ix_agents_shared_user_ids_jsonb_gin`` and
    ``ix_integrations_shared_user_ids_jsonb_gin``.

    Comparison is against the canonical UUID string. The write path normalises
    grants before storing them, so a row can only carry a non-canonical id if
    it was hand-edited; :func:`get_user_permission` normalises defensively on
    the Python side, but a JSONB key comparison cannot. The same
    normalisation (:func:`normalize_user_grants`, the one writer) is why no
    row holds a value the cast refuses, so the predicate stays bare.
    """
    jsonb_col = model_cls.shared_user_ids.cast(JSONB)
    return jsonb_col.op("?|")(cast([str(user_id)], ARRAY(SAText)))


def _company_managed_visible(model_cls: type[VisibilityQueryable]):
    """Company-managed entities, minus those narrowed to explicit grants.

    ``is_company_managed`` means "visible to every user"; ``access_restricted``
    withdraws exactly that blanket clause, leaving owner + user grants + group
    grants to decide.

    ``IS NOT TRUE`` rather than ``IS FALSE`` so a NULL reads as unrestricted.
    A NULL means the flag was never configured — the column can exist
    bare-nullable on databases where ``_ensure_model_columns`` created it ahead
    of the migration — and an unconfigured restriction must not retroactively
    hide every company-managed entity on those installs.
    """
    return and_(
        model_cls.is_company_managed.is_(True),
        model_cls.access_restricted.isnot(True),
    )


def hidden_rows_excluded(model_cls: Any):
    """The clause excluding the rows no listing shows anyone, admins included,
    or ``None`` when *model_cls* has none: another primitive's internal storage
    (``is_internal``, #3074) and Platform Health's sandbox copies (``is_sandbox``,
    #8255). The owning primitive reads such a row by id; its own row is the
    authorization. The Platform Health fleet (``health_role``) is never
    chattable while Platform Health is not available."""
    clauses = [
        getattr(model_cls, column).is_(False)
        for column in ("is_internal", "is_sandbox")
        if hasattr(model_cls, column)
    ]
    if hasattr(model_cls, "health_role"):
        from app.services.health.availability import health_enabled

        if not health_enabled():
            clauses.append(model_cls.health_role.is_(None))
    return and_(*clauses) if clauses else None


def user_visible_filter[T: VisibilityQueryable](
    model_cls: type[T],
    user_id: uuid.UUID,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
):
    """Return a SQLAlchemy filter clause for entities visible to a user.

    Visibility = own + user-shared + group-shared + company-managed, where the
    company-managed tier is skipped for entities narrowed via
    ``access_restricted``.

    When *is_admin* is ``True``, all entities are visible (no filtering).
    Admins deliberately bypass ``access_restricted`` too: it scopes an entity
    to an audience, it is not a way to keep platform administrators out of
    data they can already reach through the admin surfaces.

    When *user_group_ids* is provided (non-empty), also includes entities
    shared with any of those groups via ``shared_group_ids``.

    This is the single choke point for the three-tier model — every list,
    fetch and resolution path in the API, chat and assignment engines routes
    through it, so widening the predicate here widens them all consistently
    rather than leaving a surface that filters on the old rules.

    An entity marked ``is_internal`` is excluded for EVERYONE, admins
    included. ``is_admin`` widens which entities you may see; it does not turn
    storage owned by another primitive into content. The owning primitive
    reads it by id — its own row is the authorization.

    Putting that exclusion here covers every path that routes through this
    helper, which is most of them — but NOT all: a query that selects by owner
    or by id without this predicate bypasses it, and
    ``users.list_user_knowledge_bases`` is one such route (it repeats the
    predicate explicitly). Adding a surface that lists knowledge bases means
    checking which of the two it is.
    """
    internal = hidden_rows_excluded(model_cls)
    if is_admin:
        from sqlalchemy import true as sa_true

        return internal if internal is not None else sa_true()

    conditions = [
        model_cls.user_id == user_id,
        _company_managed_visible(model_cls),
        _shared_user_ids_contains(model_cls, user_id),
    ]
    if user_group_ids:
        conditions.append(_shared_group_ids_overlap(model_cls, user_group_ids))
    visible = or_(*conditions)
    if (parent := visibility_parent(model_cls)) is not None:
        visible = or_(
            and_(parent[0].is_(None), visible),
            parent_visible_clause(model_cls, user_id, user_group_ids),
        )
    return and_(internal, visible) if internal is not None else visible


def visible_to_user_column_clause(model_cls: type[VisibilityQueryable], user_id_column: Any):
    """:func:`user_visible_filter` for the person a COLUMN names, in SQL.

    For a background query that checks, row by row, whether each row's own
    person can still see an entity (a filed conversation's owner and its
    workspace): the same tiers - an administrator, the owner, a company-managed
    row not narrowed by ``access_restricted``, a per-person grant, a grant to
    one of the person's groups - correlated to *user_id_column* instead of bound
    to one id. A model declaring a ``visibility_parent`` is not supported.
    """
    from sqlalchemy import exists

    return or_(
        and_(
            # Correlated explicitly, as the group tier is: nested under another
            # subquery, auto-correlation would add the table of
            # *user_id_column* to this FROM and match any administrator.
            exists().where(User.id == user_id_column, User.role == "admin").correlate_except(User),
            *([hidden] if (hidden := hidden_rows_excluded(model_cls)) is not None else []),
        ),
        shared_with_user_column_clause(model_cls, user_id_column),
    )


def shared_with_user_column_clause(model_cls: type[VisibilityQueryable], user_id_column: Any) -> Any:
    """:func:`visible_to_user_column_clause` without the administrator tier.

    The entity is the person's own, company-managed and not narrowed, or
    shared with them or with one of their groups: what they open because it
    was GIVEN to them, never because their role reaches everything. A grant
    that hands work to an agent (a work item filed for another person's
    agent) reads this one, so an administrator's role is no standing
    channel into a colleague's private agent.
    """
    from sqlalchemy import exists

    person_grant = model_cls.shared_user_ids.cast(JSONB).op("?")(cast(user_id_column, SAText))
    if not jsonb_cast_indexed(model_cls.shared_user_ids):
        person_grant = when_json_parseable(model_cls.shared_user_ids, person_grant)
    group_grant = model_cls.shared_group_ids.cast(JSONB).op("?")(cast(GroupMember.group_id, SAText))
    if not jsonb_cast_indexed(model_cls.shared_group_ids):
        group_grant = when_json_parseable(model_cls.shared_group_ids, group_grant)
    visible = or_(
        model_cls.user_id == user_id_column,
        _company_managed_visible(model_cls),
        person_grant,
        # Correlated to the enclosing query explicitly: nested under another
        # subquery, auto-correlation would add the table of *user_id_column*
        # to this FROM and match any of its rows (#890).
        exists().where(GroupMember.user_id == user_id_column, group_grant).correlate_except(GroupMember),
    )
    if (hidden := hidden_rows_excluded(model_cls)) is not None:
        return and_(hidden, visible)
    return visible


def visibility_parent(model_cls: Any) -> tuple[Any, Any] | None:
    """``(column, parent model)`` when *model_cls* declares that a row with the
    column set takes its visibility from that parent (a ``visibility_parent``
    classmethod on the model - a workspace's own knowledge base), else
    None."""
    hook = getattr(model_cls, "visibility_parent", None)
    return hook() if callable(hook) else None


def parent_visible_clause(
    model_cls: Any, user_id: uuid.UUID, user_group_ids: list[str] | None = None
):
    """Rows of *model_cls* whose declared parent *user_id* can see.

    Derived, not mirrored - the ``assignment_visible_filter`` precedent: a copy
    of the parent's share columns would miss a later per-user share,
    ``access_restricted`` or revocation, while this follows every change on the
    next query (spec 032 FR-002). Non-admin only: an admin sees every parent,
    hence every row that follows one.
    """
    column, parent = visibility_parent(model_cls)
    return column.in_(
        select(parent.id).where(user_visible_filter(parent, user_id, user_group_ids))
    )


def bindable_knowledge_base_clause():
    """Knowledge bases that may be bound to an agent, a flow, a person's defaults
    or a workspace.

    Neither another primitive's internal storage (``is_internal``) nor a
    workspace's own knowledge base (``owner_workspace_id``): the workspace hands
    those to its turns itself, and a binding elsewhere would outlive the
    workspace's sharing. Every binding validator applies it, the company-managed
    ones included.
    """
    return and_(
        KnowledgeBase.owner_workspace_id.is_(None),
        KnowledgeBase.is_internal.is_(False),
    )


def user_visible_query[T: VisibilityQueryable](
    model_cls: type[T],
    user_id: uuid.UUID,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
):
    """Build a ``SELECT`` for entities visible to a user.

    Visibility = own + user-shared + group-shared + company-managed, per
    :func:`user_visible_filter`.
    When *is_admin* is ``True``, all entities are visible (no filtering).
    Returns a query ordered by ``created_at`` descending.
    """
    return (
        select(model_cls)
        .where(
            user_visible_filter(model_cls, user_id, user_group_ids, is_admin=is_admin)
        )
        .order_by(model_cls.created_at.desc())
    )


#: ``?scope=`` of the entity list endpoints and of the run feeds.
#:
#: ``visible`` (the default) is the library set: own, shared with the caller
#: and company-managed rows, for every caller, admins included - what
#: the library pages and the pickers list. ``all`` is every row the caller may
#: open by id: the whole platform for an admin, the same library set for anyone
#: else, so asking for it never widens a non-admin's reach. The detail pages
#: read ``all`` to show what an entity links to (an agent's integrations and
#: runs, the agents using a knowledge base), so an admin sees there every row
#: the detail routes already open for them.
EntityListScope = Literal["visible", "all"]
ENTITY_LIST_SCOPE_DESCRIPTION = (
    "visible (default): own, shared and company-managed rows. "
    "all: every row the caller may open by id (the whole platform for an admin)."
)


def list_scope_admin_bypass(scope: str | None, user: HasIsAdmin) -> bool:
    """Whether a list read with *scope* takes the admin bypass.

    ``all`` for an admin, never for anyone else (:data:`EntityListScope`).
    """
    return scope == "all" and bool(user.is_admin)


def agent_tool_admin_bypass(user: HasIsAdmin | None) -> bool:
    """Whether an agent tool acting for *user* takes the admin bypass on what
    it can see (agents, assignments, runs, traces, flows, evaluations and the
    other entities it looks up, lists or binds).

    An admin's agent reaches everything, as the REST routes do,
    unless strict admin access for agent tools (Settings > Access and security, or
    ``AGENT_TOOLS_STRICT_ADMIN_ACCESS``, which wins) keeps it to the admin's
    three tiers. The admin ROLE still counts in the permission
    gates either way (a company-managed write, the manual-trigger admin-only
    rule).

    Never inside a Platform Health run or turn, nor a sandbox copy's turn: its reach
    is its scope and consent alone, whoever owns its agent or asked for the
    work (PRD 9; ``identity.in_health_reach``).
    """
    from app.core.security_options import security_option
    from app.services.health.identity import in_health_reach

    if in_health_reach():
        return False
    return bool(getattr(user, "is_admin", False)) and not security_option("agent_tools_strict_admin_access")


def company_wide_visible_filter[T: VisibilityQueryable](model_cls: type[T]):
    """Return a SQLAlchemy filter clause for the entities a caller with no
    account may see (an anonymous channel sender, the public widget; the run of
    a company-managed assignment, a global MCP platform-key call and an
    evaluation with no owner are not, see :func:`caller_visible_filter`).

    Such a caller holds no grant, so it gets the tier every user gets without
    one: company-managed entities, minus those narrowed by
    ``access_restricted``, and never an ``is_internal`` row - the
    exclusion :func:`user_visible_filter` applies to everyone.
    """
    visible = _company_managed_visible(model_cls)
    if (parent := visibility_parent(model_cls)) is not None:
        # A row that follows a parent (a workspace's knowledge base)
        # follows a container a caller with no account never sees.
        visible = and_(parent[0].is_(None), visible)
    if (hidden := hidden_rows_excluded(model_cls)) is not None:
        return and_(hidden, visible)
    return visible


#: Work that reads with no account yet keeps main's company-managed tier.
COMPANY_MANAGED_RUN = "company_managed_run"
MCP_GLOBAL_KEY = "mcp_global_key"
OWNERLESS_EVALUATION = "ownerless_evaluation"
_KEEPS_COMPANY_MANAGED_TIER = frozenset({COMPANY_MANAGED_RUN, MCP_GLOBAL_KEY, OWNERLESS_EVALUATION})

#: Provenance of the work in progress, written only by the server code that
#: established it, never derived from a request: ``None`` outside any scope,
#: ``""`` inside an assignment run not (yet) marked, otherwise one of the
#: reasons above - the run of a company-managed assignment with no user (set
#: by the assignment engine), a call authenticated with the global MCP
#: endpoint's platform key (set by that endpoint), an evaluation with no
#: owning user (set by the evaluation engine).
_NO_ACCOUNT_PROVENANCE: ContextVar[str | None] = ContextVar(
    "no_account_provenance", default=None
)
_MISSING = object()


def company_managed_run_scoped[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Run *fn* in a run scope of its own, closed when it returns or raises.

    Inside the scope :func:`mark_company_managed_run` records what the run
    is; outside one it records nothing, so the mark can never outlive the run
    that set it.
    """

    @functools.wraps(fn)
    async def scoped(*args: P.args, **kwargs: P.kwargs) -> R:
        # The same scope bounds the Platform Health mark (``mark_health_run``).
        from app.services.health.identity import health_run_scope

        token = _NO_ACCOUNT_PROVENANCE.set("")
        try:
            with health_run_scope():
                return await fn(*args, **kwargs)
        finally:
            _NO_ACCOUNT_PROVENANCE.reset(token)

    return scoped


def mark_company_managed_run(assignment: Any, run: Any) -> bool:
    """Record whether the run in scope is a company-managed assignment run
    with no user, from the two rows the engine loaded; return it."""
    active = (
        getattr(assignment, "is_company_managed", None) is True
        and getattr(run, "user_id", _MISSING) is None
    )
    if _NO_ACCOUNT_PROVENANCE.get() is None:
        return False
    _NO_ACCOUNT_PROVENANCE.set(COMPANY_MANAGED_RUN if active else "")
    return active


@contextlib.contextmanager
def keeping_company_managed_tier(reason: str) -> Iterator[None]:
    """Scope in which a read with no account keeps main's company-managed tier.

    Opened by the code that established *reason* - the global MCP endpoint
    once the platform key verified, the evaluation engine for an evaluation
    row with no owner - and closed when the wrapped work returns or raises.
    """
    if reason not in (MCP_GLOBAL_KEY, OWNERLESS_EVALUATION):
        raise ValueError(f"not a no-account provenance: {reason!r}")
    token = _NO_ACCOUNT_PROVENANCE.set(reason)
    try:
        yield
    finally:
        _NO_ACCOUNT_PROVENANCE.reset(token)


def no_account_provenance() -> str | None:
    """Why the current work keeps main's tier with no account, or ``None``."""
    reason = _NO_ACCOUNT_PROVENANCE.get()
    return reason if reason in _KEEPS_COMPANY_MANAGED_TIER else None


def in_company_managed_run() -> bool:
    """Whether the current work is a company-managed assignment run with no user."""
    return _NO_ACCOUNT_PROVENANCE.get() == COMPANY_MANAGED_RUN


def caller_visible_filter[T: VisibilityQueryable](
    model_cls: type[T],
    user_id: uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
):
    """Return a SQLAlchemy filter clause for the entities a caller may see,
    with or without an account.

    A signed-in caller gets :func:`user_visible_filter` (own, shared with them
    or one of their groups, company-wide). A caller with no account - *user_id*
    ``None`` or any string that is not a UUID, such as the ``"anonymous"``
    sentinel, never compared against a UUID column - gets
    :func:`company_wide_visible_filter` alone, unless the server established
    that the work keeps main's tier (:func:`no_account_provenance`: the run of
    a company-managed assignment with no user, a call authenticated with the
    global MCP endpoint's platform key, an evaluation with no owning user):
    that work keeps every company-managed row, ``access_restricted`` ones
    included, as it always had. No admin bypass: pass one explicitly where it
    is meant.
    """
    if isinstance(user_id, str):
        user_id = safe_uuid(user_id)
    if user_id is not None:
        return user_visible_filter(model_cls, user_id, user_group_ids)
    if no_account_provenance() is not None:
        return model_cls.is_company_managed.is_(True)
    return company_wide_visible_filter(model_cls)


def assignment_visible_filter(
    user_id: uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
):
    """Return a SQLAlchemy filter clause for the assignments visible to a user.

    An assignment has no audience of its own: it is visible exactly when its
    parent agent is, per :func:`user_visible_filter` on ``Agent`` (own, shared
    with the user or one of their groups, company-managed unless narrowed by
    ``access_restricted``). ``Assignment.user_id`` and
    ``Assignment.is_company_managed`` mirror the agent but are not read here:
    a predicate over the mirrors misses per-user shares and ignores
    ``access_restricted``, and admits rows whose agent the caller
    cannot load - listed as "Unknown agent".

    A caller with no account (*user_id* ``None`` or a string that is not a
    UUID, such as the ``"anonymous"`` sentinel) gets the assignments of the
    agents :func:`company_wide_visible_filter` admits, in a company-managed
    run too.

    *is_admin* bypasses the check. Every assignment surface passes it: one
    assignment by id, the assignments of one agent (an admin opens a
    colleague's agent from Settings > Objects), the list across agents
    and the Schedule tab (the whole platform for an admin), the flow builder's
    node catalog and the agent tools. The Manual Triggers panel offers only the
    assignments whose agent its viewer's agent list holds
    (``lib/manual-triggers.ts``), so it never lists an "Unknown agent".
    """
    if is_admin:
        from sqlalchemy import true as sa_true

        return sa_true()
    if isinstance(user_id, str):
        user_id = safe_uuid(user_id)
    agent_visible = (
        company_wide_visible_filter(Agent)
        if user_id is None
        else user_visible_filter(Agent, user_id, user_group_ids)
    )
    return Assignment.agent_id.in_(select(Agent.id).where(agent_visible))


async def resolve_owner_names(
    db: AsyncSession,
    entities: Sequence[Any],
) -> dict[str, str]:
    """Batch-resolve ``user_id`` → ``full_name`` for a list of entities.

    Returns a mapping ``{str(user_id): full_name}`` for every distinct
    non-null ``user_id`` found on the entities.
    """
    user_ids = {e.user_id for e in entities if e.user_id is not None}
    if not user_ids:
        return {}
    result = await db.execute(
        select(User.id, User.full_name).where(User.id.in_(user_ids))
    )
    return {str(uid): name for uid, name in result.all()}


async def resolve_owner_emails(
    db: AsyncSession,
    entities: Sequence[Any],
) -> dict[str, str]:
    """Batch-resolve ``user_id`` → ``email`` for a list of entities.

    Returns a mapping ``{str(user_id): email}`` for every distinct non-null
    ``user_id`` found on the entities.
    """
    user_ids = {e.user_id for e in entities if e.user_id is not None}
    if not user_ids:
        return {}
    result = await db.execute(
        select(User.id, User.email).where(User.id.in_(user_ids))
    )
    return {str(uid): email for uid, email in result.all()}


async def build_mcp_runtime(srv: MCPServer, credential: Any = None):
    """Build an ``MCPServerRuntime`` from a DB model, decrypting secrets.

    Shared by ``mcp_servers.py``, ``general_chat.py``, and ``assignment_engine.py``.

    On a ``per_user`` OAuth server (``credential_mode``) the token sent is the
    acting person's own, *credential* (``MCPServerUserCredential``, resolved by
    ``mcp_user_credentials.credential_for_runtime``); without one the
    connection sends no token. The token stored on the server row is never
    used there: nobody is lent another person's account. On a ``shared``
    server *credential* is ignored.

    For OAuth-authenticated servers, the transports get an ``httpx.Auth`` hook
    that sends the stored access token as ``Authorization: Bearer <token>`` and
    keeps it fresh for the whole connection (``services/mcp_oauth_tokens.py``):
    refreshed when it is about to expire, and once after a ``401``. The
    token is not copied into the static headers, where it would go stale.

    A header or an environment variable stored as a vault reference is
    resolved; one that cannot be read raises ``SecretUnavailableError``.
    Only the map the transport reads is resolved - ``env`` for stdio,
    ``headers`` for a remote transport: a transport switch keeps the other one
    stored, and a reference there must not take down a server that never sends
    it. For the same reason, a stored ``Authorization`` header the OAuth hook
    replaces (kept from header auth) is never read.
    """
    import contextlib

    from app.core.security import decrypt_value
    from app.services.mcp_oauth_tokens import StoredMCPOAuthToken
    from app.services.mcp_user_credentials import is_per_user, mcp_holder_key
    from app.services.vault import SecretUnavailableError, reveal_json_field
    from app.tools.mcp_client import MCPServerRuntime

    async def _string_map(column: str, without: tuple[str, ...] = ()) -> dict[str, str] | None:
        try:
            document = await reveal_json_field(srv, column, without=without)
        except SecretUnavailableError:
            raise
        except Exception:
            return None
        if not isinstance(document, dict):
            return None
        return {k: v for k, v in document.items() if isinstance(k, str) and isinstance(v, str)}

    per_user = is_per_user(srv)
    holder = (credential if credential is not None and credential.server_id == srv.id else None) if per_user else srv
    access_token = None
    if srv.auth_mode == "oauth" and holder is not None and holder.access_token_enc:
        with contextlib.suppress(Exception):
            access_token = decrypt_value(holder.access_token_enc) or None

    stdio = srv.transport == "stdio"
    env_dict = await _string_map("env_enc") if stdio else None
    # A per_user server never sends a stored Authorization header either: it
    # would be one shared account again.
    headers_dict = None if stdio else await _string_map("headers_enc", ("Authorization",) if access_token or per_user else ())

    # OAuth mode: the hook sets the Bearer header on every request
    auth = (
        StoredMCPOAuthToken(mcp_holder_key(holder), access_token, holder.token_expires_at)
        if access_token and holder is not None
        else None
    )

    return MCPServerRuntime(
        name=srv.name,
        transport=srv.transport,
        command=srv.command,
        args=srv.args or [],
        env=env_dict,
        url=srv.url,
        headers=headers_dict,
        auth=auth,
        server_id=str(srv.id),
        is_company_managed=srv.is_company_managed,
        relay_group_id=(
            str(srv.relay_group_id) if getattr(srv, "relay_group_id", None) else None
        ),
        per_user=per_user,
        # One person's missing or refused account never opens the circuit
        # (or the auth escalation) for everyone on a per_user server.
        failure_scope=(str(holder.user_id) if per_user and holder is not None else ("anonymous" if per_user else None)),
    )


# ─── Run-budget authority ────────────────────────────────────────────


def assert_budget_within_ceiling(
    submitted: Any,
    *,
    previous: Any = None,
    user: User,
    what: str = "this agent",
) -> dict[str, Any] | None:
    """Authorise a run-budget override, returning what to store.

    The one gate that makes the platform's ``AGENT_LOOP_MAX_*`` settings mean
    "the budget a user cannot exceed on their own": a non-admin may tighten any
    dimension freely, but raising one above the platform ceiling is refused
    with a 403 naming every offending dimension at once (fixing them one 403 at
    a time is a miserable way to configure four numbers).

    ``submitted`` and ``previous`` are the raw dicts (or ``AgentBudgetConfig``
    models, or ``None``) — the caller does not have to normalise first.
    ``previous`` is what is stored today: an unchanged above-ceiling value is
    never refused, so one admin-raised budget does not lock every non-admin out
    of editing the agent's persona.

    Returns the normalised dict to persist, or ``None`` when the override is
    empty (so the column stores NULL rather than ``{}`` — "no override" has one
    representation).
    """
    from app.services.budget_policy import ceiling_violations, parse_overrides

    def _raw(value: Any) -> Any:
        if value is None:
            return None
        # Pydantic model (AgentBudgetConfig) or plain dict, indifferently.
        dump = getattr(value, "model_dump", None)
        return dump() if callable(dump) else value

    parsed = parse_overrides(_raw(submitted))
    violations = ceiling_violations(
        parsed, previous=parse_overrides(_raw(previous)), is_admin=user.is_admin
    )
    if violations:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Only an administrator can raise the run budget of {what} "
                "above the platform limit. "
                + " ".join(v.message() for v in violations)
            ),
        )
    return parsed.to_dict() or None


def lower_budget_to_ceiling(stored: Any, *, user: User) -> dict[str, Any] | None:
    """A copied run-budget override, lowered to what this caller may hold.

    For the paths that carry a budget the caller did NOT author — duplicating
    an agent, importing one, restoring a version. Refusing the whole action
    over a number somebody else typed would block routine work (a duplicate is
    how you adapt a company-managed agent) to prevent an escalation that
    lowering the value already prevents. See
    ``budget_policy.clamp_to_ceilings`` for the two-clause rule this belongs
    to, and ``clamp_max_iterations`` for the same treatment of the neighbouring
    field.

    An admin's copy is untouched: they could have typed the value anyway.
    """
    from app.services.budget_policy import clamp_to_ceilings, parse_overrides

    parsed = parse_overrides(stored)
    if not user.is_admin:
        parsed = clamp_to_ceilings(parsed)
    return parsed.to_dict() or None


async def require_allowed_token_url(token_url: str | None) -> None:
    """422 when a token URL being saved fails the token endpoint policy.

    Blank means "unset" and is left to the caller. The same policy runs again
    before every token request (``app.integrations.token_endpoint``); checking
    on save only makes the mistake visible where it is made.
    """
    if not token_url or not token_url.strip():
        return
    from app.integrations.token_endpoint import (
        TokenEndpointRefusedError,
        assert_token_endpoint_allowed,
    )

    try:
        await assert_token_endpoint_allowed(token_url)
    except TokenEndpointRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def require_allowed_base_urls(
    extra_config: dict | None, keys: list[str], *, routed: bool
) -> None:
    """422 when a base URL being saved in *extra_config* fails the base URL policy.

    *keys* are the ``extra_config`` keys holding a base URL
    (``base_url_policy.base_url_keys``); *routed*: a relay group carries the
    integration. The same policy runs again on every connection
    (``app.integrations.base_url_policy``); checking on save only makes the
    mistake visible where it is made.
    """
    if not extra_config:
        return
    from app.integrations.base_url_policy import BaseURLRefusedError, assert_base_url_allowed

    for key in keys:
        try:
            await assert_base_url_allowed(extra_config.get(key), routed=routed)
        except BaseURLRefusedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


async def require_allowed_mcp_server_url(url: str | None, *, transport: str | None, routed: bool) -> None:
    """422 when a remote MCP server URL being saved fails the MCP server URL policy.

    The request models already refused what needs no lookup; this adds the
    addresses a name resolves to, unless a relay group carries the server
    (*routed*). A stdio server has no URL to judge. The same policy runs again
    on every connection (``app.tools.mcp_destinations``).
    """
    from app.tools.mcp_destinations import (
        REMOTE_TRANSPORTS,
        MCPServerURLRefusedError,
        assert_server_url_allowed,
    )

    if transport not in REMOTE_TRANSPORTS:
        return
    try:
        await assert_server_url_allowed(url, routed=routed)
    except MCPServerURLRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validation_detail(exc: ValidationError, *, root: str) -> str:
    """Pydantic errors as one line per field: ``timeout_seconds: Input should be ...``.

    A nested object of the wrong type reads as such, not by the class checking it.
    An error on the whole value is named *root*.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or root}: "
        f"{'Input should be an object' if error['type'] == 'model_type' else error['msg']}"
        for error in exc.errors()
    )
