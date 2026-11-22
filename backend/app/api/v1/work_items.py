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

"""Work-item REST API.

Surfaces the durable work-item queue to the UI: list (with exact filters + a
derived ``stale`` filter), the stale count, the items waiting for the caller
(the dashboard notice), create/update,
complete, and delete. The heavy lifting (atomic claim, exact-match queries,
visibility, the company-managed write rule) lives in ``services/work_item.py``;
these handlers are thin. Company-managed items are read-only to non-admins here:
the service refuses the write (``services/company_managed_writes.py``) and the
route answers 403 with its reason.

Scheduled work items are listed, cancelled, rescheduled and edited by
their creator through the ``scheduled`` routes below
(``services/scheduled_work_items.py``, audited); the generic writes refuse
them with a 409 that says so.

The list, its count, the owners, the stale count and the bulk delete take
``ownership``: ``mine``, ``shared`` (alias ``company_managed``) or
``all`` (the default), the Executions vocabulary, validated the same way (an
unknown value is a 422). Operations > Work Items opens on ``mine``.

The list and the detail carry ``context``: the agents and the person
by name as the caller may see them, why the caller sees the item, where it came
from and what will pick it up (``services/work_item_context.py``).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_current_user, require_agentic_ai
from app.api.v1.helpers import (
    combine_scopes,
    fleet_filter,
    parse_uuid,
    resolve_list_ownership,
    workspace_filter,
)
from app.models.user import User
from app.services import scheduled_work_items as scheduled_service
from app.services import work_item as work_item_service
from app.services import work_item_context
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    person_writer,
)
from app.services.scheduled_work_items import ScheduledWorkItemError
from app.services.visibility import SegmentCounts, Visibility, segment_counts

router = APIRouter()


def _workspace_scope(workspace: str | None, fleet: str | None = None):
    """``?workspace=``: the items of a workspace's Operations tab.

    ``?fleet=``: the items of the fleet, the population of the agents'
    ``list_work_items`` scope ``fleet``: owned, created or claimed by a
    current home member, or filed in the fleet's workspace with no owner, so
    the tab's counts are the API's. Both compose.
    """
    from app.models.work_item import WorkItem
    from app.services.coordination_scope import fleet_work_items_clause

    in_workspace = (
        workspace_filter(
            workspace, WorkItem.workspace_id, WorkItem.owner_agent_id, WorkItem.created_by_agent_id
        )
        if workspace is not None
        else None
    )
    in_fleet = fleet_work_items_clause(parse_uuid(fleet, label="fleet")) if fleet else None
    return combine_scopes(in_workspace, in_fleet)


def _refused(exc: CompanyManagedWriteRefusedError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def _scheduled_refusal(exc: ScheduledWorkItemError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


class WorkItemCreate(BaseModel):
    title: str
    description: str | None = None
    owner_agent_id: str | None = None
    priority: str = "medium"
    status: str = "open"
    correlation_key: str | None = None
    # The deadline; it defers nothing. not_before defers the pickup (#8753).
    due_at: str | None = None
    not_before: str | None = None
    payload: dict | None = None
    is_company_managed: bool = False
    # The workspace it is filed in (#9796): read by everyone who reads that
    # workspace; the service refuses a caller below the `config` tier (403).
    workspace_id: str | None = None
    # The items it waits for: created blocked, reopened once all done.
    blocked_on: list[str] | None = None
    # The pull requests and issues it waits for, the reason, a person.
    blocked_on_refs: list[dict] | None = None
    blocked_reason: str | None = None
    needs_human: bool | None = None
    # The decision asked of a person: {question, options, recommendation, deadline}.
    ask: dict | None = None
    # Answer the unfinished item with the same key and title instead.
    dedupe: bool = False
    # "key_and_title" (default), "key" (the correlation key alone) or
    # "similar" (also a near-duplicate key or title of the same owner).
    dedupe_by: str | None = None
    # The agents its completion is routed to: {targets, kind, instructions}.
    fleet_impact: dict | None = None


def _clearable_date(body: BaseModel, field: str) -> str | None:
    """A date of an update: absent leaves it (``None``), an explicit null clears it (``""``)."""
    value = getattr(body, field)
    if value is None and field in body.model_fields_set:
        return ""
    return value


def _clearable_ask(body: BaseModel) -> dict | None:
    """The ask of an update: absent leaves it (``None``), an explicit null clears it (``{}``)."""
    value = getattr(body, "ask", None)
    if value is None and "ask" in body.model_fields_set:
        return {}
    return value


class WorkItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    owner_agent_id: str | None = None
    priority: str | None = None
    status: str | None = None
    correlation_key: str | None = None
    # An explicit null clears the deadline or the pickup time; absent leaves it.
    due_at: str | None = None
    not_before: str | None = None
    payload: dict | None = None
    # Replaces the items it waits for; [] clears the dependency.
    blocked_on: list[str] | None = None
    # Replaces the pull requests and issues it waits for; [] clears them.
    blocked_on_refs: list[dict] | None = None
    # What it waits for in words; "" clears it.
    blocked_reason: str | None = None
    needs_human: bool | None = None
    # Replaces the decision asked of a person; {} or an explicit null clears it.
    ask: dict | None = None
    # Replaces the agents its completion is routed to; {} clears it.
    fleet_impact: dict | None = None


class WorkItemComplete(BaseModel):
    result: str | None = None
    status: str = "done"


class WorkItemBulkDelete(BaseModel):
    ids: list[str] | None = None
    all_matching_filters: bool = False
    owner_agent_id: str | None = None
    status: str | None = None
    correlation_key: str | None = None
    open_only: bool = False
    stale_only: bool = False
    search: str | None = None
    scheduled_only: bool = False
    ownership: str | None = None
    vis: str | None = None


class ScheduledReschedule(BaseModel):
    """A new time, ISO-8601 WITH its UTC offset (one without is refused)."""

    due_at: str


class ScheduledActionEdit(BaseModel):
    """A replacement action for a scheduled item that has not run; same mode."""

    mode: Literal["exact", "instruction"]
    tool: str | None = None
    arguments: dict | None = None
    prompt: str | None = None
    target: str | None = None


def _time_bounds(completed_from: str | None, completed_to: str | None, updated_from: str | None) -> dict:
    """The list's time filters parsed; ``ValueError`` naming the bad one."""
    parse = work_item_service.parse_time_filter
    return {
        "completed_from": parse(completed_from, "completed_from"),
        "completed_to": parse(completed_to, "completed_to"),
        "updated_from": parse(updated_from, "updated_from"),
    }


@router.get("")
async def list_work_items(
    owner_agent_id: str | None = None,
    status: str | None = None,
    correlation_key: str | None = None,
    open_only: bool = False,
    stale_only: bool = False,
    search: str | None = None,
    scheduled_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    limit: int = 100,
    offset: int = 0,
    completed_from: str | None = None,
    completed_to: str | None = None,
    updated_from: str | None = None,
    sort: str | None = None,
    blocked_on: str | None = None,
    correlation_key_prefix: str | None = None,
    waiting_for_me: bool = False,
    order: str | None = None,
    direction: str | None = None,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """The visible work items; ``workspace`` keeps the ones filed in it or
    owned or created by a home member of the fleet it hosts;
    ``fleet`` the ones owned or created by a home member of that fleet.

    ``completed_from`` / ``completed_to`` / ``updated_from`` (ISO-8601) bound
    by time and ``sort`` is ``created_desc`` (default), ``completed_desc`` or
    ``updated_desc``; an unreadable one is a 422. ``blocked_on`` (a work item
    id) keeps the items waiting for it; a value that is not one matches none.
    ``correlation_key_prefix`` keeps the items whose key starts with it.
    ``waiting_for_me`` keeps the items waiting for the caller, oldest first:
    exactly what ``GET /waiting-for-me`` counts.

    ``order`` (``created``, ``updated``, ``due``, ``priority``, ``status``,
    ``owner``, ``completed``) with ``direction`` (``asc`` / ``desc``) is the
    Operations table's column sort and wins over ``sort``; an unknown one
    keeps ``sort``."""
    try:
        sort_order = work_item_service.normalize_sort(sort)
        bounds = _time_bounds(completed_from, completed_to, updated_from)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    items = await work_item_service.list_work_items(
        **bounds,
        sort=sort_order,
        order=order,
        direction=direction,
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        scope=_workspace_scope(workspace, fleet),
        owner_agent_id=owner_agent_id,
        status=status,
        correlation_key=correlation_key,
        open_only=open_only,
        stale_only=stale_only,
        search=search,
        scheduled_only=scheduled_only,
        blocked_on=blocked_on or None,
        correlation_key_prefix=correlation_key_prefix or None,
        waiting_for_me=waiting_for_me,
        limit=limit,
        offset=offset,
    )
    return await work_item_context.describe_work_items(items, viewer=user)


@router.get("/status-counts")
async def count_work_items_by_status(
    owner_agent_id: str | None = None,
    correlation_key: str | None = None,
    stale_only: bool = False,
    search: str | None = None,
    scheduled_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    waiting_for_me: bool = False,
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    """``{status: count}`` under the list's filters minus the status: the
    numbers of the Operations table's Status filter, one grouped query.
    Declared before ``GET /{work_item_id}``."""
    return await work_item_service.count_work_items_by_status(
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        scope=_workspace_scope(workspace, fleet),
        owner_agent_id=owner_agent_id,
        correlation_key=correlation_key,
        stale_only=stale_only,
        search=search,
        scheduled_only=scheduled_only,
        waiting_for_me=waiting_for_me,
    )


@router.get("/graph")
async def work_items_graph(
    owner_agent_id: str | None = None,
    status: str | None = None,
    correlation_key: str | None = None,
    open_only: bool = False,
    stale_only: bool = False,
    search: str | None = None,
    scheduled_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    waiting_for_me: bool = False,
    user: User = Depends(get_current_user),
) -> dict:
    """The Graph view of Tasks > Work Items: ``{nodes, edges, truncated}``
    for the list's filters, read with the list's visibility
    (``services/work_item_graph.py``): the items, the visible items they
    wait for outside the filter, their references and the people they wait
    for. Declared before ``GET /{work_item_id}``."""
    from app.services import work_item_graph

    return await work_item_graph.read_graph(
        user_id=str(user.id),
        viewer=user,
        filters={
            "ownership": resolve_list_ownership(vis, ownership),
            "scope": _workspace_scope(workspace, fleet),
            "owner_agent_id": owner_agent_id,
            "status": status,
            "correlation_key": correlation_key,
            "open_only": open_only,
            "stale_only": stale_only,
            "search": search,
            "scheduled_only": scheduled_only,
            "waiting_for_me": waiting_for_me,
        },
    )


@router.get("/owners")
async def list_work_item_owners(
    status: str | None = None,
    correlation_key: str | None = None,
    open_only: bool = False,
    stale_only: bool = False,
    search: str | None = None,
    scheduled_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    waiting_for_me: bool = False,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """The owner agents of the items the list's filters match, with counts.

    ``workspace`` and ``fleet`` narrow like on the list, so a fleet's
    agent filter offers the owners its items actually have.

    What an agent filter offers: each owner the caller may open by name, the
    unassigned items (``agent: null``), and the items of agents the caller
    cannot open counted together. Declared before ``GET /{work_item_id}``.
    """
    counts = await work_item_service.count_work_items_by_owner(
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        status=status,
        correlation_key=correlation_key,
        open_only=open_only,
        stale_only=stale_only,
        search=search,
        scheduled_only=scheduled_only,
        scope=_workspace_scope(workspace, fleet),
        waiting_for_me=waiting_for_me,
    )
    return await work_item_context.describe_owners(counts, viewer=user)


@router.get("/scheduled")
async def list_scheduled_work_items(
    agent_id: str | None = None,
    state: str = "upcoming",
    limit: int = 100,
    user: User = Depends(get_current_user),
    fleet: str | None = None,
) -> list[dict]:
    """The caller's scheduled work items, with their agent's name and icon.

    The one-off half of the fleet's "what will happen and when" view (Agentic
    Flow > Schedule, beside ``GET /assignments/schedule``) and of an agent's
    Overview "Coming up" card (``agent_id``). ``fleet`` narrows to a fleet's
    home members, or ``none`` to the agents in no fleet, like the
    assignment schedule. Declared before ``GET /{work_item_id}`` so
    ``scheduled`` is never read as an id.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.agent import Agent
    from app.models.work_item import WorkItem

    owner_scope = fleet_filter(WorkItem.owner_agent_id, fleet) if fleet is not None else None
    try:
        items = await scheduled_service.list_scheduled_work_items(
            user_id=str(user.id), agent_id=agent_id, state=state, limit=limit, owner_scope=owner_scope
        )
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    agent_ids = {i["owner_agent_id"] for i in items if i.get("owner_agent_id")}
    agents: dict[str, tuple[str, str | None]] = {}
    if agent_ids:
        async with async_session() as db:
            rows = await db.execute(
                select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(agent_ids))
            )
            agents = {str(row[0]): (row[1], row[2]) for row in rows.all()}
    for item in items:
        name, icon = agents.get(item.get("owner_agent_id") or "", (None, None))
        item["agent_name"] = name
        item["agent_icon"] = icon
    return items


@router.post("/{work_item_id}/cancel")
async def cancel_scheduled_work_item(
    work_item_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    """Cancel one of the caller's scheduled work items before it runs (audited)."""
    try:
        item = await scheduled_service.cancel_scheduled_work_item(
            work_item_id, user_id=str(user.id), via="rest"
        )
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Scheduled work item not found")
    return item


@router.post("/{work_item_id}/reschedule")
async def reschedule_scheduled_work_item(
    work_item_id: str,
    body: ScheduledReschedule,
    user: User = Depends(get_current_user),
) -> dict:
    """Move one of the caller's scheduled work items that has not run (audited)."""
    try:
        due = scheduled_service.parse_due_at(body.due_at, tz_name=user.timezone)
        item = await scheduled_service.reschedule_scheduled_work_item(
            work_item_id, due, user_id=str(user.id), via="rest"
        )
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Scheduled work item not found")
    return item


@router.put("/{work_item_id}/action")
async def edit_scheduled_action(
    work_item_id: str,
    body: ScheduledActionEdit,
    user: User = Depends(get_current_user),
) -> dict:
    """Replace the action of one of the caller's waiting scheduled items (audited)."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.agent import Agent
    from app.services.scheduled_work_item_runner import check_exact_action

    current = await scheduled_service.get_scheduled_work_item(work_item_id, user_id=str(user.id))
    if current is None:
        raise HTTPException(status_code=404, detail="Scheduled work item not found")
    try:
        action = scheduled_service.build_action(
            body.mode,
            tool=body.tool,
            arguments=body.arguments,
            prompt=body.prompt,
            target=body.target,
        )
        if action["mode"] == "exact" and current.get("owner_agent_id"):
            pinned = ((current.get("payload") or {}).get("action") or {}).get("pinned_integrations")
            if pinned:
                action["pinned_integrations"] = pinned
            async with async_session() as db:
                agent = (
                    await db.execute(select(Agent).where(Agent.id == current["owner_agent_id"]))
                ).scalar_one_or_none()
                if agent is None:
                    raise ScheduledWorkItemError("Its agent no longer exists.", status_code=409)
                action, refusal = await check_exact_action(db, agent, str(user.id), action)
            if refusal:
                raise ScheduledWorkItemError(refusal, status_code=422)
        item = await scheduled_service.edit_scheduled_action(
            work_item_id, action, user_id=str(user.id), via="rest"
        )
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Scheduled work item not found")
    return item


@router.get("/count")
async def count_work_items(
    owner_agent_id: str | None = None,
    status: str | None = None,
    correlation_key: str | None = None,
    open_only: bool = False,
    stale_only: bool = False,
    search: str | None = None,
    deletable_only: bool = False,
    scheduled_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    completed_from: str | None = None,
    completed_to: str | None = None,
    updated_from: str | None = None,
    blocked_on: str | None = None,
    correlation_key_prefix: str | None = None,
    waiting_for_me: bool = False,
    user: User = Depends(get_current_user),
) -> dict:
    """Total work items matching the filters (powers pagination + select-all).

    The time bounds are the list's, so its total counts what its pages hold.

    ``deletable_only=True`` counts only the rows the caller could bulk delete
    (own private rows; company-managed too for admins), so the "select all N
    matching" UI never promises more than ``bulk-delete`` will actually remove.
    """
    try:
        bounds = _time_bounds(completed_from, completed_to, updated_from)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    count = await work_item_service.count_work_items(
        **bounds,
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        owner_agent_id=owner_agent_id,
        status=status,
        correlation_key=correlation_key,
        open_only=open_only,
        stale_only=stale_only,
        search=search,
        is_admin=user.is_admin,
        deletable_only=deletable_only,
        scheduled_only=scheduled_only,
        scope=_workspace_scope(workspace, fleet),
        blocked_on=blocked_on or None,
        correlation_key_prefix=correlation_key_prefix or None,
        waiting_for_me=waiting_for_me,
    )
    return {"count": count}


@router.get("/segment-counts", response_model=SegmentCounts)
async def work_item_segment_counts(
    owner_agent_id: str | None = None,
    status: str | None = None,
    correlation_key: str | None = None,
    open_only: bool = False,
    stale_only: bool = False,
    search: str | None = None,
    scheduled_only: bool = False,
    workspace: str | None = None,
    fleet: str | None = None,
    waiting_for_me: bool = False,
    user: User = Depends(get_current_user),
) -> SegmentCounts:
    """How many work items each visibility segment holds under the list's other filters."""

    async def count(segment: Visibility) -> int:
        return await work_item_service.count_work_items(
            user_id=str(user.id),
            ownership=resolve_list_ownership(segment, None),
            owner_agent_id=owner_agent_id,
            status=status,
            correlation_key=correlation_key,
            open_only=open_only,
            stale_only=stale_only,
            search=search,
            is_admin=user.is_admin,
            scheduled_only=scheduled_only,
            scope=_workspace_scope(workspace, fleet),
            waiting_for_me=waiting_for_me,
        )

    return await segment_counts(count)


@router.get("/stale-count")
async def stale_work_item_count(
    ownership: str | None = None,
    vis: str | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    count = await work_item_service.count_stale_work_items(
        user_id=str(user.id), ownership=resolve_list_ownership(vis, ownership)
    )
    return {"count": count}


@router.get("/waiting-for-me")
async def work_items_waiting_for_me(user: User = Depends(get_current_user)) -> dict:
    """The home dashboard notice: the caller's items that wait for the caller.

    ``{"count", "oldest"}``: a decision the caller owes (``needs_human``) or an
    item of theirs with no owner agent open past due; an agent's own waits are
    never counted (``work_items_waiting_for_person``).
    """
    return await work_item_service.work_items_waiting_for_person(user_id=str(user.id))


@router.post("/bulk-delete")
async def bulk_delete_work_items(
    body: WorkItemBulkDelete,
    user: User = Depends(get_current_user),
) -> dict:
    """Delete multiple work items (own private rows; admins also company-managed).

    Provide ``ids`` for specific items or ``all_matching_filters=True`` with the
    same filters as the list endpoint. Company-managed rows are only removed for
    admins, matching the single-delete gate.
    """
    if not body.ids and not body.all_matching_filters:
        raise HTTPException(
            status_code=400,
            detail="Provide either ids or set all_matching_filters=True",
        )
    deleted = await work_item_service.bulk_delete_work_items(
        user_id=str(user.id),
        is_admin=user.is_admin,
        ids=body.ids,
        all_matching_filters=body.all_matching_filters,
        owner_agent_id=body.owner_agent_id,
        status=body.status,
        correlation_key=body.correlation_key,
        open_only=body.open_only,
        stale_only=body.stale_only,
        search=body.search,
        scheduled_only=body.scheduled_only,
        ownership=resolve_list_ownership(body.vis, body.ownership),
    )
    return {"deleted": deleted}


@router.get("/{work_item_id}")
async def get_work_item(
    work_item_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    item = await work_item_service.get_work_item(work_item_id, user_id=str(user.id))
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    described = (await work_item_context.describe_work_items([item], viewer=user))[0]
    return {
        **described,
        "roi": await _roi_block(described, user),
        "attachments": await _attachments_block(described),
        "note_runs": await _note_runs_block(described, user),
    }


async def _note_runs_block(item: dict[str, object], user: User) -> dict[str, dict[str, object]]:
    """The agent behind each stamped note's run, for the dialog's activity timeline (#776)."""
    from app.core.database import async_session
    from app.services.work_item_note_runs import note_runs

    async with async_session() as db:
        description = item.get("description")
        return await note_runs(db, description if isinstance(description, str) else None, user)


async def _attachments_block(item: dict) -> dict:
    """The files the item carries, by name, for its dialog (#12092).

    Read through the item's own visibility, the grant ``get_work_item`` uses:
    ``files`` are the stored ones in the order attached, ``gone`` counts the
    ones no longer stored.
    """
    from app.services.work_item_files import work_item_file_refs

    refs, gone = await work_item_file_refs(item)
    return {
        "files": [
            {"file_id": r.file_id, "filename": r.filename, "content_type": r.content_type, "size": r.size} for r in refs
        ],
        "gone": gone,
    }


async def _roi_block(item: dict, user: User) -> dict | None:
    """The item's ROI record for the outcome review row (``per_work_item``), or ``None``."""
    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.services.roi_records import review_block

    item_id = safe_uuid(item.get("id"))
    if item_id is None:
        return None
    async with async_session() as db:
        return await review_block(
            db,
            work_item_id=item_id,
            user_id=user.id,
            is_admin=user.is_admin,
            subject_owner_id=safe_uuid(item.get("user_id")),
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_work_item(
    body: WorkItemCreate,
    user: User = Depends(get_current_user),
) -> dict:
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="Title is required")
    try:
        return await work_item_service.create_work_item(
            user_id=str(user.id),
            title=body.title,
            description=body.description,
            owner_agent_id=body.owner_agent_id,
            priority=body.priority,
            status=body.status,
            correlation_key=body.correlation_key,
            due_at=body.due_at,
            not_before=body.not_before,
            payload=body.payload,
            source="manual",
            is_company_managed=body.is_company_managed,
            workspace_id=parse_uuid(body.workspace_id, label="workspace_id") if body.workspace_id else None,
            blocked_on=body.blocked_on,
            blocked_on_refs=body.blocked_on_refs,
            blocked_reason=body.blocked_reason,
            needs_human=body.needs_human,
            ask=body.ask,
            dedupe=body.dedupe,
            dedupe_by=body.dedupe_by,
            fleet_impact=body.fleet_impact,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{work_item_id}")
async def update_work_item(
    work_item_id: str,
    body: WorkItemUpdate,
    user: User = Depends(get_current_user),
) -> dict:
    try:
        item = await work_item_service.update_work_item(
            work_item_id,
            user_id=str(user.id),
            title=body.title,
            description=body.description,
            owner_agent_id=body.owner_agent_id,
            priority=body.priority,
            status=body.status,
            correlation_key=body.correlation_key,
            due_at=_clearable_date(body, "due_at"),
            not_before=_clearable_date(body, "not_before"),
            payload=body.payload,
            blocked_on=body.blocked_on,
            blocked_on_refs=body.blocked_on_refs,
            blocked_reason=body.blocked_reason,
            needs_human=body.needs_human,
            ask=_clearable_ask(body),
            fleet_impact=body.fleet_impact,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.post("/{work_item_id}/complete")
async def complete_work_item(
    work_item_id: str,
    body: WorkItemComplete,
    user: User = Depends(get_current_user),
) -> dict:
    try:
        item = await work_item_service.complete_work_item(
            work_item_id,
            user_id=str(user.id),
            result=body.result,
            status=body.status,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


class WorkItemAnswer(BaseModel):
    # The key of one of the ask's options; text alone answers a legacy item.
    option: str | None = None
    text: str | None = None


@router.post("/{work_item_id}/answer")
async def answer_work_item(
    work_item_id: str,
    body: WorkItemAnswer,
    user: User = Depends(get_current_user),
) -> dict:
    """Answer the decision an item waits for: recorded, needs_human cleared,
    the item back in its owner's queue (``work_item.answer_work_item``)."""
    try:
        item = await work_item_service.answer_work_item(
            work_item_id,
            user_id=str(user.id),
            option=body.option,
            text=body.text,
            answered_by=getattr(user, "full_name", None) or getattr(user, "email", None),
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    except work_item_service.WorkItemNotWaitingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


class WorkItemAsk(BaseModel):
    action: Literal["recheck", "solved", "do"]
    # It's solved: what changed, in the person's words (optional).
    text: str | None = None


@router.post("/{work_item_id}/ask")
async def ask_work_item(
    work_item_id: str,
    body: WorkItemAsk,
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai),
) -> dict:
    """Send Re-check, It's solved or Do it to the item's agent (#774): a stamped
    note, then its inbox or a run of the agent takes the item now
    (``work_item.ask_work_item``). Answers ``{item, agent, via}``."""
    try:
        sent = await work_item_service.ask_work_item(
            work_item_id,
            user_id=str(user.id),
            action=body.action,
            text=body.text,
            asked_by=getattr(user, "full_name", None) or getattr(user, "email", None),
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    except work_item_service.WorkItemAskRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if sent is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    return sent


@router.post("/{work_item_id}/requeue")
async def requeue_work_item(
    work_item_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    """Put an item back in the queue: open, unclaimed, dispatch attempts reset.

    For a claim nobody finishes, an item auto-blocked after failed dispatches,
    or a finished item to do again (``work_item.requeue_work_item``).
    """
    try:
        item = await work_item_service.requeue_work_item(
            work_item_id,
            user_id=str(user.id),
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.delete("/{work_item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_item(
    work_item_id: str,
    user: User = Depends(get_current_user),
) -> None:
    try:
        deleted = await work_item_service.delete_work_item(
            work_item_id,
            user_id=str(user.id),
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ScheduledWorkItemError as exc:
        raise _scheduled_refusal(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Work item not found")
