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

"""Shared-state REST API.

Surfaces the generic cross-run shared state store to the UI: list, get-by-id,
upsert (create-or-merge), and delete. The agent-facing read/write path is the
``shared_state`` built-in tools; this REST surface is for human visibility and
cleanup. Company-managed entries are read-only to non-admins here: the service
refuses the write (``services/company_managed_writes.py``) and the route answers
403 with its reason.

The list, its count and the bulk delete take ``ownership``: ``mine``,
``shared`` (alias ``company_managed``) or ``all`` (the default), the
Executions vocabulary, validated the same way. Operations > Shared State opens
on ``mine``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    combine_scopes,
    parse_uuid,
    resolve_list_ownership,
    workspace_filter,
)
from app.models.user import User
from app.services import shared_state as shared_state_service
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    person_writer,
)
from app.services.visibility import SegmentCounts, Visibility, segment_counts

router = APIRouter()


def _refused(exc: CompanyManagedWriteRefusedError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


class SharedStateUpsert(BaseModel):
    key: str
    label: str | None = None
    owner_agent_id: str | None = None
    value: dict | None = None
    note: str | None = None
    status: str | None = None
    is_company_managed: bool = False
    replace_value: bool = False
    # Lists under top-level keys changed on the server: items to
    # append, and matches to remove (equal, or an object carrying the fields).
    append: dict[str, list] | None = None
    remove: dict[str, list] | None = None
    # Then keep only the newest N items of each named list.
    keep_last: dict[str, int] | None = None
    # Per list, the field(s) identifying an object item: an append replaces
    # the item with the same values instead of adding a second one.
    unique_by: dict[str, str | list[str]] | None = None
    # Per list, a patch (or patches) on the object items a match selects:
    # {"match": {...}, "set": {...}, "increment": {...}, "append": {...}}.
    update: dict[str, dict | list[dict]] | None = None


class SharedStateBulkDelete(BaseModel):
    ids: list[str] | None = None
    all_matching_filters: bool = False
    search: str | None = None
    status: str | None = None
    stale_only: bool = False
    ownership: str | None = None
    vis: str | None = None


class SharedStateResolve(BaseModel):
    note: str | None = None


@router.get("")
async def list_shared_state(
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    owner_agent_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str | None = None,
    direction: str | None = None,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """The visible entries; ``workspace`` keeps the ones filed in it or owned
    by a home member of the fleet it hosts; ``fleet`` the ones owned
    by a home member of that fleet; ``owner_agent_id`` the ones that
    agent owns. ``sort`` (``key``, ``owner``, ``updated``, ``size``) and
    ``direction`` (``asc`` / ``desc``) order the page; omitted, the newest
    updated first (the most neglected first under ``stale_only``)."""
    return await shared_state_service.list_states(
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        scope=_workspace_scope(workspace, fleet, owner_agent_id),
        search=search,
        status=status,
        stale_only=stale_only,
        limit=limit,
        offset=offset,
        sort=sort,
        direction=direction,
    )


@router.get("/count")
async def count_shared_state(
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    deletable_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    owner_agent_id: str | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    """Total shared-state entries matching the filters (powers pagination).

    ``deletable_only=True`` counts only the rows the caller could bulk delete
    (own private rows; company-managed too for admins), so the "select all N
    matching" UI never promises more than ``bulk-delete`` will actually remove.
    """
    count = await shared_state_service.count_states(
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        search=search,
        status=status,
        stale_only=stale_only,
        is_admin=user.is_admin,
        deletable_only=deletable_only,
        scope=_workspace_scope(workspace, fleet, owner_agent_id),
    )
    return {"count": count}


@router.get("/segment-counts", response_model=SegmentCounts)
async def shared_state_segment_counts(
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    workspace: str | None = None,
    fleet: str | None = None,
    owner_agent_id: str | None = None,
    user: User = Depends(get_current_user),
) -> SegmentCounts:
    """How many entries each visibility segment holds under the list's other filters.

    Declared before ``GET /{state_id}`` so the literal wins the match.
    """

    async def count(segment: Visibility) -> int:
        return await shared_state_service.count_states(
            user_id=str(user.id),
            ownership=resolve_list_ownership(segment, None),
            search=search,
            status=status,
            stale_only=stale_only,
            is_admin=user.is_admin,
            scope=_workspace_scope(workspace, fleet, owner_agent_id),
        )

    return await segment_counts(count)


@router.get("/owners")
async def list_shared_state_owners(
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """The owner agents of the entries the list's filters match, with counts.

    What the owner-agent filter of Operations > Shared State offers, as
    ``GET /work-items/owners`` does for Work Items: each owner the caller may
    open by name, the entries no agent owns (``agent: null``), and the entries
    of agents the caller cannot open counted together. ``workspace`` and
    ``fleet`` narrow like on the list, so a fleet page offers its members'
    entries only. Declared before ``GET /{state_id}``.
    """
    from app.services import work_item_context

    counts = await shared_state_service.count_states_by_owner(
        user_id=str(user.id),
        ownership=resolve_list_ownership(vis, ownership),
        search=search,
        status=status,
        stale_only=stale_only,
        scope=_workspace_scope(workspace, fleet),
    )
    return await work_item_context.describe_owners(counts, viewer=user)


def _workspace_scope(
    workspace: str | None,
    fleet: str | None = None,
    owner_agent_id: str | None = None,
):
    """``?workspace=``: the entries of a workspace's Operations tab.

    ``?fleet=``: the entries of the fleet: owned by a current home
    member, or filed in its workspace with no owner, the population of the
    agents' ``list_shared_state`` scope ``fleet``. ``?owner_agent_id=``:
    the entries that agent owns; a value that is not a UUID is a 422
    naming the parameter. All compose and
    only narrow the caller's visible entries.
    """
    from app.models.shared_state import SharedState
    from app.services.coordination_scope import fleet_shared_states_clause

    in_workspace = (
        workspace_filter(workspace, SharedState.workspace_id, SharedState.owner_agent_id)
        if workspace is not None
        else None
    )
    in_fleet = fleet_shared_states_clause(parse_uuid(fleet, label="fleet")) if fleet else None
    of_owner = (
        SharedState.owner_agent_id == parse_uuid(owner_agent_id, label="owner_agent_id")
        if owner_agent_id
        else None
    )
    return combine_scopes(in_workspace, in_fleet, of_owner)


@router.post("/bulk-delete")
async def bulk_delete_shared_state(
    body: SharedStateBulkDelete,
    user: User = Depends(get_current_user),
) -> dict:
    """Delete multiple shared-state entries (own private rows; admins also company-managed).

    Provide ``ids`` for specific entries or ``all_matching_filters=True`` with the
    same ``search`` / ``status`` / ``stale_only`` filters as the list endpoint, so
    "select all N matching" removes exactly what the filtered list shows.
    Company-managed rows are only removed for admins, matching the single-delete
    gate.
    """
    if not body.ids and not body.all_matching_filters:
        raise HTTPException(
            status_code=400,
            detail="Provide either ids or set all_matching_filters=True",
        )
    deleted = await shared_state_service.bulk_delete_states(
        user_id=str(user.id),
        is_admin=user.is_admin,
        ids=body.ids,
        all_matching_filters=body.all_matching_filters,
        search=body.search,
        status=body.status,
        stale_only=body.stale_only,
        ownership=resolve_list_ownership(body.vis, body.ownership),
    )
    return {"deleted": deleted}


@router.get("/{state_id}")
async def get_shared_state(
    state_id: str,
    user: User = Depends(get_current_user),
) -> dict:
    state = await shared_state_service.get_state_by_id(
        user_id=str(user.id), state_id=state_id
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Shared state not found")
    return state


@router.post("", status_code=status.HTTP_201_CREATED)
async def upsert_shared_state(
    body: SharedStateUpsert,
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return await shared_state_service.upsert_state(
            user_id=str(user.id),
            key=body.key,
            label=body.label,
            owner_agent_id=body.owner_agent_id,
            value=body.value,
            note=body.note,
            status=body.status,
            is_company_managed=body.is_company_managed,
            replace_value=body.replace_value,
            append=body.append,
            remove=body.remove,
            keep_last=body.keep_last,
            unique_by=body.unique_by,
            update=body.update,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{state_id}/resolve")
async def resolve_shared_state(
    state_id: str,
    body: SharedStateResolve | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    """Mark a specific shared-state entry resolved (terminal) by its id.

    Resolving by id (not key) targets the exact row the UI is showing: several
    visible rows - the caller's own plus company-managed ones - can share a key,
    so a key-addressed resolve could flip a different row than the one clicked.
    Returns 404 when no visible entry has that id, so a resolve that races a
    delete / purge never creates a phantom resolved entry, and 403 for a
    company-managed entry and a non-admin.
    """
    try:
        resolved = await shared_state_service.resolve_state_by_id(
            user_id=str(user.id),
            state_id=state_id,
            note=body.note if body else None,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="Shared state not found")
    return resolved


@router.delete("/{state_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shared_state(
    state_id: str,
    user: User = Depends(get_current_user),
) -> None:
    try:
        deleted = await shared_state_service.delete_state_by_id(
            user_id=str(user.id),
            state_id=state_id,
            writer=person_writer(is_admin=user.is_admin),
        )
    except CompanyManagedWriteRefusedError as exc:
        raise _refused(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Shared state not found")
