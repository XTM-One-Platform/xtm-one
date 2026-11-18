# SPDX-License-Identifier: Apache-2.0

"""Admin Objects – unified, paginated list of all platform entities for troubleshooting."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    case,
    cast,
    false,
    func,
    literal,
    literal_column,
    null,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import TypeEngine

from app.api.deps import require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.feature_flags import is_feature_enabled
from app.core.security_options import security_option
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment, AssignmentRun
from app.models.browser_profile import BrowserProfile
from app.models.channel_bot import ChannelBot
from app.models.chat import Conversation
from app.models.custom_tool import CustomTool
from app.models.dataset import Dataset
from app.models.evaluation import Evaluation
from app.models.file_attachment import FileAttachment
from app.models.fleet import Fleet
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.report import Report
from app.models.runner import Runner
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.variable import Variable
from app.models.video_studio import VideoProject
from app.models.work_item import WorkItem
from app.models.workset import Workset
from app.models.workspace import Workspace
from app.services.evaluation.availability import evaluation_enabled
from app.services.runners import runner_status_expression

router = APIRouter()


class PlatformObjectItem(BaseModel):
    id: str
    name: str
    object_type: str
    user_id: str | None
    user_email: str | None
    user_name: str | None
    is_company_managed: bool
    created_at: datetime
    updated_at: datetime
    # Per-type detail (``DETAIL_COLUMNS``): what an object of that type IS at a
    # glance, ``None`` on the types that have no such notion. The runner arm
    # says a machine's OS family, whether it is online / offline /
    # revoked, when it was last seen and the build it runs; other arms put
    # their own state and kind in ``status`` / ``subtype``.
    status: str | None = None
    subtype: str | None = None
    last_seen_at: datetime | None = None
    version: str | None = None


class PlatformObjectListResponse(BaseModel):
    items: list[PlatformObjectItem]
    total: int


class EntityArm(NamedTuple):
    """One entity table of the union."""

    model: type
    #: The row's ``object_type`` - doubles as the filter value AND the key the
    #: frontend's ``PlatformObjectType`` union and ``OBJECT_TYPE_CONFIG`` use.
    label: str
    #: The attribute surfaced as the row's ``name`` - most entities call it
    #: ``name``; a deck, a video, a report or a conversation carry a ``title``.
    name_column: str = "name"
    #: Optional per-type detail columns (keys of ``DETAIL_COLUMNS``), built at
    #: request time so an expression can read the clock. The other arms carry
    #: typed NULLs in those positions, which is what keeps the union aligned.
    details: Callable[[datetime], dict[str, ColumnElement]] | None = None
    #: Whether the type is offered on this deployment (``None`` = always). A
    #: switched-off subsystem's rows could not be opened from the browser, so
    #: they are not listed either.
    enabled: Callable[[], bool] | None = None
    #: The row's ``name`` when no single column is it (a run is named after
    #: its assignment), replacing ``name_column``.
    name: Callable[[], ColumnElement] | None = None
    #: The row's ``updated_at`` when the table keeps none (a run's last
    #: transition, a file's upload), replacing ``updated_at``.
    updated: Callable[[], ColumnElement] | None = None
    #: Whether the unfiltered list unions it. The types whose tables grow with
    #: usage rather than with configuration (conversations, runs, files, work
    #: items, flow runs) are listed when their type is picked, so the default
    #: page keeps reading configuration objects only.
    listed_by_default: bool = True
    #: Rows of the table that are not objects of this type (another primitive's
    #: internal storage): excluded from the list and from every count.
    where: Callable[[], ColumnElement[bool]] | None = None


# The per-type detail positions every arm carries, with the SQL type of the
# NULL an arm without that notion contributes (a union needs every position
# typed the same way). Mirrored by the optional fields of ``PlatformObjectItem``.
DETAIL_COLUMNS: dict[str, TypeEngine] = {
    "status": String(),
    "subtype": String(),
    "last_seen_at": DateTime(timezone=True),
    "version": String(),
}


def _runner_details(now: datetime) -> dict[str, ColumnElement]:
    """What a machine is at a glance.

    Its OS family, whether it is online / offline / revoked by the SAME reading
    as the Desktop page and the ``GET /runners`` serializer (``last_seen_at``
    recency through :func:`runner_status_expression`, never the cached
    ``status`` column), when it was last seen, and the runner build it runs -
    because "deployed" means "connected and current", not "a row exists".
    """
    return {
        "status": runner_status_expression(now),
        "subtype": Runner.platform,
        "last_seen_at": Runner.last_seen_at,
        "version": Runner.agent_version,
    }


def _run_name() -> ColumnElement:
    """A run is named after its assignment; a run with none (a chat handover) by its kind."""
    assignment_name = (
        select(Assignment.name).where(Assignment.id == AssignmentRun.assignment_id).scalar_subquery()
    )
    return func.coalesce(assignment_name, literal("Assignment run"))


def _flow_run_name() -> ColumnElement:
    return func.coalesce(
        select(Flow.name).where(Flow.id == FlowRun.flow_id).scalar_subquery(), literal("Flow run")
    )


def _last_transition(model: type) -> Callable[[], ColumnElement]:
    return lambda: func.coalesce(model.completed_at, model.started_at, model.created_at)


def _entity_select(arm: EntityArm, *, now: datetime | None = None):
    """Build a SELECT for one entity type with the unified column set."""
    model = arm.model
    details = arm.details(now or datetime.now(UTC)) if arm.details else {}
    unknown = set(details) - set(DETAIL_COLUMNS)
    if unknown:
        raise ValueError(f"{arm.label}: unknown detail columns {sorted(unknown)}")
    company_managed = (
        model.is_company_managed if hasattr(model, "is_company_managed") else cast(false(), Boolean)
    )
    columns = [
        model.id.cast(String).label("id"),
        (arm.name() if arm.name else getattr(model, arm.name_column)).label("name"),
        literal_column(f"'{arm.label}'").label("object_type"),
        model.user_id.cast(String).label("user_id"),
        company_managed.label("is_company_managed"),
        model.created_at.label("created_at"),
        (arm.updated() if arm.updated else model.updated_at).label("updated_at"),
    ]
    for column, sql_type in DETAIL_COLUMNS.items():
        expression = details.get(column)
        if expression is None:
            expression = cast(null(), sql_type)
        columns.append(expression.label(column))
    statement = select(*columns)
    return statement.where(arm.where()) if arm.where else statement


# Every entity type the browser unions, keyed by the ``object_type`` filter value.
# Each ``label`` doubles as the value the frontend's ``PlatformObjectType`` union
# and ``OBJECT_TYPE_CONFIG`` key on, so a new entry here needs its counterpart
# there (pinned by ``tests/test_admin_objects.py``). The library pages list
# only what the caller owns, is shared with, or the company published - admins
# included - so this browser is where an admin finds a colleague's
# private object, and a type missing here is a type an admin cannot reach at all.
# The ``platform_insights`` agent tools count and list the same arms.
ENTITY_MAP: dict[str, EntityArm] = {
    "agent": EntityArm(Agent, "agent"),
    # A workset's corpus is an ``is_internal`` knowledge base: its storage, not
    # a collection. It has no page (every read hides it, admins included), so
    # it is listed under its Workset row only, never as a Knowledge Base.
    "knowledge_base": EntityArm(KnowledgeBase, "knowledge_base", where=lambda: KnowledgeBase.is_internal.is_(False)),
    "prompt": EntityArm(Prompt, "prompt"),
    "skill": EntityArm(AgentSkill, "skill"),
    "mcp_server": EntityArm(MCPServer, "mcp_server"),
    "custom_tool": EntityArm(CustomTool, "custom_tool"),
    "integration": EntityArm(Integration, "integration"),
    "presentation": EntityArm(Presentation, "presentation", name_column="title"),
    # A video (Video Studio) is a deliverable like a deck, shared the
    # same three-tier way, and titled like one.
    "video": EntityArm(VideoProject, "video", name_column="title"),
    "template_container": EntityArm(TemplateContainer, "template_container"),
    # Enrolled machines - XTM One Desktop installs and headless runners alike.
    # The Desktop page lists a PER-USER fleet (own + shared +
    # company-managed, admins included), and that page defers
    # cross-user fleet oversight to THIS browser: how many machines are
    # deployed, enrolled by whom, and whether they are still connected. Off
    # with the subsystem, like the Desktop page (``GET /runners/{id}`` 404s).
    "runner": EntityArm(
        Runner,
        "runner",
        details=_runner_details,
        enabled=lambda: settings.runners_enabled,
    ),
    # A flow opens in the builder, which the ``FLOW_BUILDER`` flag gates.
    "flow": EntityArm(Flow, "flow", enabled=lambda: is_feature_enabled("FLOW_BUILDER")),
    # Evaluations and their datasets, while evaluation is on.
    "dataset": EntityArm(Dataset, "dataset", enabled=evaluation_enabled),
    "evaluation": EntityArm(Evaluation, "evaluation", enabled=evaluation_enabled),
    # Disabled custom variables (Settings > Access and security, or
    # ``DISABLE_CUSTOM_VARIABLES``, which wins) refuse every variable route.
    "variable": EntityArm(
        Variable, "variable", enabled=lambda: not security_option("disable_custom_variables")
    ),
    "channel_bot": EntityArm(ChannelBot, "channel_bot"),
    # More arms: what an admin asks about once configuration is
    # found - the work it produced and the places it lives.
    "assignment": EntityArm(
        Assignment,
        "assignment",
        details=lambda _now: {
            "status": case((Assignment.enabled.is_(True), literal("enabled")), else_=literal("paused")),
            "subtype": Assignment.trigger_type,
        },
    ),
    "assignment_run": EntityArm(
        AssignmentRun,
        "assignment_run",
        name=_run_name,
        updated=_last_transition(AssignmentRun),
        details=lambda _now: {"status": AssignmentRun.status, "subtype": AssignmentRun.failure_cause},
        listed_by_default=False,
    ),
    "conversation": EntityArm(
        Conversation,
        "conversation",
        name_column="title",
        details=lambda _now: {"subtype": Conversation.channel},
        listed_by_default=False,
    ),
    "report": EntityArm(Report, "report", name_column="title"),
    "file": EntityArm(
        FileAttachment,
        "file",
        name_column="filename",
        updated=lambda: FileAttachment.created_at,
        details=lambda _now: {"status": FileAttachment.source, "subtype": FileAttachment.content_type},
        listed_by_default=False,
    ),
    "workspace": EntityArm(Workspace, "workspace"),
    # A fleet (a team of agents), archived ones included and marked so; a
    # deleted fleet is gone from the table and lives on in the audit log only.
    # Only "archived" is a status here: whether a fleet runs (not set up,
    # partly on, on, paused) is computed by the Fleets list, never "active".
    "fleet": EntityArm(
        Fleet,
        "fleet",
        details=lambda _now: {
            "status": case((Fleet.is_archived.is_(True), literal("archived"))),
        },
    ),
    "workset": EntityArm(
        Workset,
        "workset",
        name=lambda: func.coalesce(Workset.label, Workset.key),
        details=lambda _now: {"status": Workset.status},
    ),
    "work_item": EntityArm(
        WorkItem,
        "work_item",
        name_column="title",
        details=lambda _now: {"status": WorkItem.status, "subtype": WorkItem.source},
        listed_by_default=False,
    ),
    "flow_run": EntityArm(
        FlowRun,
        "flow_run",
        name=_flow_run_name,
        updated=_last_transition(FlowRun),
        details=lambda _now: {"status": FlowRun.status, "subtype": FlowRun.trigger_type},
        enabled=lambda: is_feature_enabled("FLOW_BUILDER"),
        listed_by_default=False,
    ),
    "browser_profile": EntityArm(
        BrowserProfile,
        "browser_profile",
        details=lambda _now: {"status": BrowserProfile.session_status},
    ),
}


def active_entity_arms() -> dict[str, EntityArm]:
    """The arms offered on this deployment."""
    return {
        key: arm
        for key, arm in ENTITY_MAP.items()
        if arm.enabled is None or arm.enabled()
    }


def objects_union(arms: list[EntityArm], *, now: datetime):
    """The unified rows of *arms* as one subquery ``objects`` (one table: no union)."""
    if len(arms) == 1:
        combined = _entity_select(arms[0], now=now)
    else:
        combined = union_all(*(_entity_select(arm, now=now) for arm in arms))
    return combined.subquery("objects")


#: The orders the list offers (``sort``), each with its direction (``direction``);
#: ties are broken by type then id so a page boundary never repeats or skips a row.
OBJECT_SORTS = ("created_at", "updated_at", "name", "object_type", "owner", "status")


def _values(raw: str | None) -> list[str]:
    """One value or several separated by commas, blanks dropped, order kept."""
    return [value.strip() for value in (raw or "").split(",") if value.strip()]


def selected_arms(object_type: str | None) -> list[EntityArm]:
    """The arms a type filter reads: the types named (one or several), else the default union.

    A type this deployment does not offer is ignored; when none of the named
    types is offered the list reads the configuration types, as before.
    """
    arms = active_entity_arms()
    picked = [arms[key] for key in dict.fromkeys(_values(object_type)) if key in arms]
    return picked or [arm for arm in arms.values() if arm.listed_by_default]


def _owner_ids(user_id: str | None) -> list[str]:
    """The owner ids of the filter that are UUIDs; anything else is ignored."""
    owners: list[str] = []
    for value in _values(user_id):
        try:
            uuid.UUID(value)
        except ValueError:
            continue
        owners.append(value)
    return owners


def _narrowed(sub, *, search: str | None, user_id: str | None, status: str | None, is_company_managed: bool | None):
    """The ``WHERE`` clauses of the filters, on the ``objects`` subquery."""
    clauses = []
    if search:
        # Escape the LIKE metacharacters so a name containing ``%`` or ``_`` is
        # matched literally rather than as a wildcard.
        needle = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(func.lower(sub.c.name).like(f"%{needle}%", escape="\\"))
    owners = _owner_ids(user_id)
    if owners:
        clauses.append(sub.c.user_id.in_(owners))
    statuses = _values(status)
    if statuses:
        clauses.append(sub.c.status.in_(statuses))
    if is_company_managed is not None:
        clauses.append(sub.c.is_company_managed.is_(is_company_managed))
    return clauses


@router.get("", response_model=PlatformObjectListResponse)
async def list_platform_objects(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(
        None, description="Filter by name (case-insensitive substring)"
    ),
    object_type: str | None = Query(None, description="Filter by entity type, one or several comma-separated"),
    user_id: str | None = Query(None, description="Filter by owner user ID, one or several comma-separated"),
    status: str | None = Query(None, description="Filter by status, one or several comma-separated"),
    is_company_managed: bool | None = Query(
        None, description="Only company-managed (true) or personal (false) objects"
    ),
    sort: str = Query("created_at", description="created_at, updated_at, name, object_type, owner or status"),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all platform objects (admin-only). Supports filtering, sorting and pagination."""

    # One clock for the whole statement, so every arm that reads it (the
    # runner's online window) agrees with the others and with the response.
    now = datetime.now(UTC)

    # One type picked reads its table alone, several their union; otherwise the
    # union of the configuration types (``listed_by_default``).
    sub = objects_union(selected_arms(object_type), now=now)

    # Join with users to get email/name
    user_alias = select(
        User.id.cast(String).label("uid"),
        User.email.label("user_email"),
        User.full_name.label("user_name"),
    ).subquery("users")

    query = select(
        sub.c.id,
        sub.c.name,
        sub.c.object_type,
        sub.c.user_id,
        user_alias.c.user_email,
        user_alias.c.user_name,
        sub.c.is_company_managed,
        sub.c.created_at,
        sub.c.updated_at,
        *(sub.c[column] for column in DETAIL_COLUMNS),
    ).outerjoin(user_alias, sub.c.user_id == user_alias.c.uid)

    count_query = select(func.count()).select_from(sub)

    # object_type filtering is applied earlier by building the union of the
    # picked arms only; the other filters narrow the subquery.
    for clause in _narrowed(sub, search=search, user_id=user_id, status=status, is_company_managed=is_company_managed):
        query = query.where(clause)
        count_query = count_query.where(clause)

    # Count total
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Order and paginate. ``created_at`` alone is not a total order across the
    # union arms - the built-in rows every boot seeds (template containers,
    # skills, ...) share a timestamp to the microsecond - so a page boundary
    # falling inside such a tie would repeat or skip rows between pages. The
    # type + id tiebreakers make the paging deterministic. An unknown sort
    # reads as the default (newest first).
    column = {
        "created_at": sub.c.created_at,
        "updated_at": sub.c.updated_at,
        "name": func.lower(sub.c.name),
        "object_type": sub.c.object_type,
        "owner": func.lower(func.coalesce(user_alias.c.user_name, user_alias.c.user_email)),
        "status": sub.c.status,
    }.get(sort if sort in OBJECT_SORTS else "created_at", sub.c.created_at)
    # A row without a value (no owner, no status) sorts last either way.
    ordered = column.asc().nulls_last() if direction == "asc" else column.desc().nulls_last()
    query = query.order_by(ordered, sub.c.object_type, sub.c.id).offset(offset).limit(limit)
    result = await db.execute(query)
    rows = result.all()

    items = [
        PlatformObjectItem(
            id=row.id,
            name=row.name,
            object_type=row.object_type,
            user_id=row.user_id,
            user_email=row.user_email,
            user_name=row.user_name,
            is_company_managed=row.is_company_managed,
            created_at=row.created_at,
            updated_at=row.updated_at,
            status=row.status,
            subtype=row.subtype,
            last_seen_at=row.last_seen_at,
            version=row.version,
        )
        for row in rows
    ]

    return PlatformObjectListResponse(items=items, total=total)
