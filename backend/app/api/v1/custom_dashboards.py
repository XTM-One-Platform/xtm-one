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

"""Custom dashboard API - read, rearrange, share and restore agent-built dashboards.

Dashboards are BUILT by the agent tools (``build_dashboard`` / ``edit_dashboard``
in ``services/builtin_tools/custom_dashboard.py``); there is no create route. A
person may rename a dashboard, remove, move or edit a widget from the studio's
inspector (``PATCH``, revision-checked and validated through the same
``apply_operations`` as the tools, so no client stores a widget the validator
never saw), share it, restore an earlier version and delete it.

Access is ``custom_dashboards.load_dashboard_for`` on every route: the owner;
a ``config`` grantee edits and shares, a ``read_only`` grantee reads; a
company-managed dashboard is read by everyone and edited by admins; an admin
reads anyone else's dashboard (``is_admin``) and changes none. A dashboard one
may not read is the 404 of an unknown id; one read but not editable is a 403.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_agentic_ai
from app.api.v1.helpers import parse_path_id
from app.api.v1.schemas.custom_dashboard import (
    CustomDashboardCreate,
    CustomDashboardImportResponse,
    CustomDashboardResponse,
    CustomDashboardSharingUpdate,
    CustomDashboardSummaryResponse,
    CustomDashboardTagsUpdate,
    CustomDashboardUpdate,
    CustomDashboardVersionResponse,
    DashboardDefinitionUpdate,
    DashboardHubShareRequest,
    DashboardPublicLinkListItem,
    DashboardPublicLinkRequest,
    DashboardPublicLinkResponse,
    DashboardPublicLinkUpdate,
    DashboardPublishRequest,
    DashboardRenderExportRequest,
    DashboardRestoreRequest,
    DashboardVersionDiffResponse,
    PlatformFormsResponse,
    PlatformSchemaRequest,
    PlatformSchemaResponse,
    WidgetDataRequest,
    WidgetDataResponse,
    WidgetPreviewRequest,
)
from app.core.database import get_db
from app.core.feature_flags import require_feature_or_404
from app.core.rate_limit import enforce_rate_limit
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.services.audit import log_audit
from app.services.custom_dashboard_spec import MAX_TITLE_CHARS, DashboardSpecError, validate_widget
from app.services.custom_dashboards import (
    DELETE_TIERS,
    EDIT_TIERS,
    DashboardRevisionConflictError,
    DashboardTier,
    clean_tags,
    dashboard_permission,
    diff_dashboard_versions,
    duplicate_dashboard,
    edit_dashboard,
    empty_definition,
    get_dashboard_version,
    list_dashboard_versions,
    list_visible_dashboards,
    load_dashboard_for,
    restore_dashboard_version,
    save_dashboard,
    serialize_dashboard,
    serialize_version,
    sharing_fields,
)

#: 404 while the Dashboard Studio is off: every dashboard route is the studio's,
#: the dashboards an agent built included (nothing of them is offered then).
_require_studio = require_feature_or_404("DASHBOARD_STUDIO")

router = APIRouter(dependencies=[Depends(_require_studio)])
logger = logging.getLogger(__name__)

#: A dashboard list is a navigation aid, not a dataset.
MAX_LIST_RESULTS = 100

_NOT_FOUND = "Dashboard not found"


async def _load(
    db: AsyncSession, user: User, dashboard_id: str, *, need: Literal["read", "edit"] = "read", for_update: bool = False
) -> tuple[CustomDashboard, DashboardTier]:
    """The dashboard and the caller's tier: 404 when they may not read it, 403 when they may not edit it."""
    doc_id = parse_path_id(dashboard_id, not_found_detail=_NOT_FOUND)
    found = await load_dashboard_for(db, doc_id, user_id=user.id, is_admin=user.is_admin, for_update=for_update)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    dashboard, tier = found
    if need == "edit" and tier not in EDIT_TIERS:
        detail = (
            "Only admins can change a company dashboard"
            if dashboard.is_company_managed
            else "You can view this dashboard but not change it"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return dashboard, tier


def _response(dashboard: CustomDashboard, tier: DashboardTier) -> CustomDashboardResponse:
    manage = tier in EDIT_TIERS
    return CustomDashboardResponse(
        **serialize_dashboard(dashboard),
        **(sharing_fields(dashboard) if manage else {}),
        can_manage=manage,
        permission=tier,
    )


@router.get("", response_model=list[CustomDashboardSummaryResponse])
async def list_custom_dashboards(
    limit: int = Query(default=50, ge=1, le=MAX_LIST_RESULTS),
    scope: Literal["all", "mine", "shared", "company"] = Query(default="all"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The dashboards the caller may open (their own, shared with them, company ones), most recent first."""
    rows = await list_visible_dashboards(db, user_id=user.id, scope=scope, limit=limit)
    return [
        CustomDashboardSummaryResponse(**serialize_dashboard(row, include_spec=False), permission=tier)
        for row, tier in rows
    ]


@router.post("", response_model=CustomDashboardResponse, status_code=status.HTTP_201_CREATED)
async def create_custom_dashboard(
    body: CustomDashboardCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """An empty dashboard of the caller's, to fill in the studio."""
    try:
        dashboard = await save_dashboard(
            db, user_id=user.id, spec_payload=empty_definition(body.title, body.description), source="manual", summary="created"
        )
    except DashboardSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db, user=user, action="create", entity_type="custom_dashboard", entity_id=dashboard.id, entity_name=dashboard.title,
        summary=f"Created dashboard '{dashboard.title}'", request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(dashboard)
    return _response(dashboard, "owner")


@router.post(
    "/import", response_model=CustomDashboardImportResponse, status_code=status.HTTP_201_CREATED
)
async def import_custom_dashboard(
    request: Request,
    file: UploadFile = File(...),
    title: Annotated[str | None, Form(max_length=MAX_TITLE_CHARS)] = None,
    bindings: Annotated[str | None, Form(max_length=2000)] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A dashboard of the caller's from a file (an XTM One pack, an OpenCTI dashboard, custom view or widget
    export, an OpenAEV dashboard export, at most 5 MB), each platform widget bound to an integration of its
    platform they can use (*bindings*: a JSON object of platform to integration id, when they have several)."""
    from app.services.dashboard_interop import MAX_FILE_BYTES
    from app.services.dashboard_widget_packs import PackError, import_dashboard

    chosen = _bindings(bindings)
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is larger than 5 MB.")
    try:
        dashboard, notes = await import_dashboard(db, user_id=user.id, data=data, filename=file.filename or "", title=title, bindings=chosen)
    except (PackError, DashboardSpecError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db, user=user, action="create", entity_type="custom_dashboard", entity_id=dashboard.id, entity_name=dashboard.title,
        summary=f"Imported dashboard '{dashboard.title}'", details={"widgets": dashboard.widget_count}, request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(dashboard)
    return CustomDashboardImportResponse(**_response(dashboard, "owner").model_dump(), import_notes=notes)


def _bindings(raw: str | None) -> dict[str, str] | None:
    """The platform-to-integration choices of an import: a JSON object of short strings, or a 422."""
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict) or len(value) > 10 or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 40 and len(v) <= 60 for k, v in value.items()):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="bindings must map each platform to an integration id.")
    return value


@router.get(
    "/public-links", response_model=list[DashboardPublicLinkListItem], dependencies=[Depends(require_admin)]
)
async def list_dashboard_public_links(db: AsyncSession = Depends(get_db)):
    """Every published dashboard link (administrators), newest first: what anyone holding a URL can see, to review and
    turn off. The links themselves are not listed: a manager copies one from its dashboard."""
    from sqlalchemy import select

    from app.models.custom_dashboard import CustomDashboardPublicLink

    rows = (
        await db.execute(
            select(CustomDashboardPublicLink, CustomDashboard.title, User.full_name)
            .join(CustomDashboard, CustomDashboard.id == CustomDashboardPublicLink.dashboard_id)
            .outerjoin(User, User.id == CustomDashboardPublicLink.created_by_user_id)
            .order_by(CustomDashboardPublicLink.created_at.desc())
            .limit(500)
        )
    ).all()
    return [
        DashboardPublicLinkListItem(
            dashboard_id=str(link.dashboard_id), dashboard_title=title, publisher_name=publisher, refresh_minutes=link.refresh_minutes,
            created_at=link.created_at.isoformat() if link.created_at else None,
            snapshot_at=link.snapshot_at.isoformat() if link.snapshot_at else None,
        )
        for link, title, publisher in rows
    ]  # fmt: skip


@router.delete(
    "/public-links/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)]
)
async def turn_off_dashboard_public_link(
    dashboard_id: str, request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> Response:
    """Turn any published link off (administrators, from the review list): its address stops answering at once."""
    from app.services.dashboard_public_links import OUTCOME_DISABLED, audit_details, disable_public_link

    dashboard = await db.get(CustomDashboard, parse_path_id(dashboard_id, not_found_detail=_NOT_FOUND))
    if dashboard is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if await disable_public_link(db, dashboard.id):
        await log_audit(
            db, user=user, action="update", entity_type="custom_dashboard", entity_id=dashboard.id, entity_name=dashboard.title,
            summary=f"Published link of dashboard '{dashboard.title}' turned off by an administrator",
            details=audit_details(OUTCOME_DISABLED), request=request,
        )  # fmt: skip
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/platform-forms", response_model=PlatformFormsResponse)
async def read_platform_forms(user: User = Depends(get_current_user)):
    """How each platform's widget editor is built: the descriptions the studio's one source form draws.

    The same for everyone (``dashboard_platform.forms``, the platforms' adapters);
    what a form lists from a platform is asked through ``/platform-schema``.
    """
    from app.services.dashboard_platform.forms import platform_forms

    return {"forms": platform_forms()}


@router.get("/{dashboard_id}", response_model=CustomDashboardResponse)
async def get_custom_dashboard(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One dashboard with its spec, and who it is shared with for those who may change that."""
    from app.api.v1.helpers import get_user_group_ids

    dashboard, tier = await _load(db, user, dashboard_id)
    response = _response(dashboard, tier)
    # Readable as themselves (not through an admin's reach): may be their /dashboard.
    response.can_use_as_home = (
        dashboard_permission(dashboard, user_id=user.id, user_group_ids=await get_user_group_ids(db, user.id))
        is not None
    )
    return response


@router.post(
    "/{dashboard_id}/widgets/preview",
    response_model=WidgetDataResponse,
)
async def preview_platform_widget(
    dashboard_id: str,
    body: WidgetPreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Draw a platform widget that is not saved yet, as the caller sees it.

    Edit access to the dashboard (the studio's inspector, before saving); the
    widget is checked exactly as a write would check it, and read with the
    caller's own access to its integration.
    """
    from app.services.dashboard_live import read_widget_data, request_for

    await _load(db, user, dashboard_id, need="edit")
    if not isinstance(body.widget, dict) or body.widget.get("kind") != "platform":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Only a platform widget is previewed.")
    try:
        widget = validate_widget(body.widget, where="widget")
    except DashboardSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return await read_widget_data(db, user.id, request_for(widget, body.range))


@router.post("/platform-schema", response_model=PlatformSchemaResponse)
async def read_platform_schema(
    body: PlatformSchemaRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What an OpenCTI / OpenAEV source editor needs (entity types, filter keys, attributes, values).

    Read through an integration the caller may use, with their own access
    there (``dashboard_platform_schema``); no dashboard is involved. A refusal
    is a fixed code.
    """
    from app.services.dashboard_platform_schema import read_schema

    params = body.model_dump(include={"perspective", "types", "key", "search", "ids"})
    return await read_schema(db, user.id, body.integration_id, body.source, body.kind, params)


@router.post(
    "/{dashboard_id}/widgets/{widget_id}/data",
    response_model=WidgetDataResponse,
)
async def read_platform_widget_data(
    dashboard_id: str,
    widget_id: str,
    body: WidgetDataRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The live data of one platform widget of the dashboard, read with the caller's own access.

    The widget is read from the stored dashboard, never from the request: the
    body only picks a time range.
    """
    return await _widget_data(db, user, dashboard_id, widget_id, body)


async def _widget_data(db: AsyncSession, user: User, dashboard_id: str, widget_id: str, body: WidgetDataRequest):
    """A visualization's data, read from its stored primary source with *user*'s own access."""
    from app.services.dashboard_definition import upgrade

    dashboard, _tier = await _load(db, user, dashboard_id)
    definition = upgrade(dashboard.spec)
    viz = (definition.get("visualizations") or {}).get(widget_id)
    sid = (viz.get("dataSources") or {}).get("primary") if isinstance(viz, dict) else None
    if not sid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Widget not found")
    return await _source_data(db, user, dashboard.id, definition, sid, body)


@router.post(
    "/{dashboard_id}/data-sources/{source_id}/data",
    response_model=WidgetDataResponse,
)
async def read_data_source(
    dashboard_id: str,
    source_id: str,
    body: WidgetDataRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The data of one data source of the dashboard, read with the caller's own access."""
    return await _data_source(db, user, dashboard_id, source_id, body)


async def _data_source(db: AsyncSession, user: User, dashboard_id: str, source_id: str, body: WidgetDataRequest):
    from app.services.dashboard_definition import upgrade

    dashboard, _tier = await _load(db, user, dashboard_id)
    definition = upgrade(dashboard.spec)
    return await _source_data(db, user, dashboard.id, definition, source_id, body)


async def _source_data(
    db: AsyncSession, user: User, dashboard_id: object, definition: dict, source_id: str, body: WidgetDataRequest
):
    """A stored data source's data, read with *user*'s own access and the page's tokens (``dashboard_sources.read_source``)."""
    from app.services.dashboard_sources import read_source

    if source_id not in (definition.get("dataSources") or {}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    return await read_source(
        db, user.id, definition, source_id, body.range, dashboard_id=dashboard_id, tokens=body.tokens, fresh=body.fresh
    )


#: A source refreshed on the server is read again on request at most this often (seconds).
REFRESH_NOW_FLOOR_SECONDS = 30


@router.post(
    "/{dashboard_id}/data-sources/{source_id}/refresh",
    response_model=WidgetDataResponse,
)
async def refresh_data_source(
    dashboard_id: str,
    source_id: str,
    body: WidgetDataRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Refresh now: read a source refreshed on the server again, as its publisher (``dashboard_refresh``).

    Read access to the dashboard is enough, as for the page's own refresh: the
    read is the publisher's, whoever presses the button, and it is made at most
    every 30 seconds. A live source answers 404 (its page reads it itself).
    """
    from datetime import UTC, datetime

    from app.core.database import async_session
    from app.models.custom_dashboard import CustomDashboardSourceSnapshot
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_refresh import refresh_row, snapshot_row_id

    dashboard, _tier = await _load(db, user, dashboard_id)
    row_id = await snapshot_row_id(db, dashboard.id, source_id)
    if row_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This data source is not refreshed on the server.")
    row = await db.get(CustomDashboardSourceSnapshot, row_id)
    recent = row is not None and row.refreshed_at is not None and (
        datetime.now(UTC) - row.refreshed_at
    ).total_seconds() < REFRESH_NOW_FLOOR_SECONDS
    if not recent:
        await refresh_row(async_session, row_id)
        if row is not None:
            await db.refresh(row)
    return await _source_data(db, user, dashboard.id, upgrade(dashboard.spec), source_id, body)


@router.post(
    "/{dashboard_id}/data-sources/{source_id}/ask",
    response_model=WidgetDataResponse,
)
async def ask_data_source(
    dashboard_id: str,
    source_id: str,
    # The user first: the AI gate refuses a member of an AI-disabled group by
    # the person the user dependency binds (``tests/test_group_ai_gate_order.py``).
    user: User = Depends(get_current_user),
    _ai: None = Depends(require_agentic_ai),
    db: AsyncSession = Depends(get_db),
):
    """Ask an ``ask`` source's question as the caller, to an agent they may chat with.

    Read access to the dashboard is enough: the question is the dashboard's,
    the run is the caller's own (their agent access, their quota). The answer
    is read through the data routes once the run ends. A refusal is a fixed
    code; at most a few questions a minute per person, refused when the count
    cannot be kept.
    """
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_sources import AskRefusedError, ask_agent

    dashboard, _tier = await _load(db, user, dashboard_id)
    definition = upgrade(dashboard.spec)
    if source_id not in (definition.get("dataSources") or {}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    try:
        await ask_agent(db, user.id, dashboard.id, source_id, definition)
    except AskRefusedError as exc:
        return WidgetDataResponse(status="error", code=exc.code)
    return WidgetDataResponse(status="error", code="running")


@router.patch("/{dashboard_id}", response_model=CustomDashboardResponse)
async def update_custom_dashboard(
    dashboard_id: str,
    body: CustomDashboardUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename the dashboard, remove or move widgets, at the revision the client read.

    409 when the dashboard moved on (an agent refreshed it meanwhile): the
    client reloads and the person sees what changed before editing again.
    """
    dashboard, tier = await _load(db, user, dashboard_id, need="edit", for_update=True)
    try:
        await edit_dashboard(
            db,
            dashboard,
            operations=[op.model_dump(exclude_none=True, by_alias=True) for op in body.operations],
            title=body.title,
            subtitle=body.subtitle,
            expected_revision=body.revision,
            acting_user_id=user.id,
            source="manual",
        )
    except DashboardRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DashboardSpecError as exc:
        # The message names the widget and the field (ids and catalog rules the
        # client sent or can see), never anything of another user.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Updated dashboard '{dashboard.title}'",
        details={"revision": dashboard.revision, "operations": len(body.operations)},
        request=request,
    )
    await db.commit()
    await db.refresh(dashboard)
    return _response(dashboard, tier)


@router.put(
    "/{dashboard_id}/definition", response_model=CustomDashboardResponse
)
async def replace_custom_dashboard_definition(
    dashboard_id: str,
    body: DashboardDefinitionUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write the whole definition (the source editor), validated like an agent's, at the revision read.

    The manifest the editor shows (``dashboard_native``), or a version 2 definition.
    """
    from app.services.dashboard_native import is_native

    dashboard, tier = await _load(db, user, dashboard_id, need="edit", for_update=True)
    try:
        await save_dashboard(
            db,
            user_id=user.id,
            spec_payload=body.definition if is_native(body.definition) else {**body.definition, "version": 2},
            dashboard=dashboard,
            expected_revision=body.revision,
            source="manual",
            summary="edited the definition",
        )
    except DashboardRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DashboardSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Edited the definition of dashboard '{dashboard.title}'",
        details={"revision": dashboard.revision},
        request=request,
    )
    await db.commit()
    await db.refresh(dashboard)
    return _response(dashboard, tier)


#: What a dashboard asset answers with: shown as an image, never run as a document.
ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


@router.post("/{dashboard_id}/assets", status_code=status.HTTP_201_CREATED)
async def upload_custom_dashboard_asset(
    dashboard_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload an image or an SVG a canvas of the dashboard will draw (edit access).

    Closed to PNG, JPEG, GIF, WebP and SVG (cleaned by ``svg_sanitizer``),
    5 MB each, 100 per dashboard (``services/dashboard_assets.py``); a refusal
    is a 422 saying which rule, never what the file held.
    """
    from app.services.dashboard_assets import MAX_ASSET_BYTES, DashboardAssetError, store_asset

    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    data = await file.read(MAX_ASSET_BYTES + 1)
    try:
        row = await store_asset(
            db,
            dashboard_id=dashboard.id,
            user_id=user.id,
            data=data,
            filename=file.filename or "image",
            content_type=file.content_type,
        )
    except DashboardAssetError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Added a file to dashboard '{dashboard.title}'",
        details={"file_id": str(row.id), "content_type": row.content_type, "size": row.size},
        request=request,
    )
    await db.commit()
    return {"id": str(row.id), "filename": row.filename, "content_type": row.content_type, "size": row.size}


@router.get("/{dashboard_id}/assets/{file_id}")
async def read_custom_dashboard_asset(
    dashboard_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """One file a canvas of the dashboard draws (read access).

    Served iff it was uploaded to THIS dashboard and its definition draws it
    now; anything else is the unknown-file 404, so the route never tells which
    ids exist.
    """
    return await _asset(db, user, dashboard_id, file_id)


async def _asset(db: AsyncSession, user: User, dashboard_id: str, file_id: str) -> Response:
    from app.services.dashboard_assets import load_drawn_asset
    from app.services.dashboard_definition import upgrade
    from app.services.file_access import file_not_found
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    dashboard, _tier = await _load(db, user, dashboard_id)
    row = await load_drawn_asset(db, dashboard.id, upgrade(dashboard.spec), file_id)
    if row is None:
        raise file_not_found()
    try:
        data = await file_storage.download(row.s3_object_name)
    except FileNotFoundInStorageError:
        raise file_not_found() from None
    headers = file_response_headers(row, store=False)
    headers["Content-Security-Policy"] = ASSET_CSP
    return Response(content=data, media_type=row.content_type, headers=headers)


@router.put("/{dashboard_id}/sharing", response_model=CustomDashboardResponse)
async def share_custom_dashboard(
    dashboard_id: str,
    body: CustomDashboardSharingUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change who may open or edit the dashboard (its owner or a ``config`` grantee).

    The same write as a deck's or a video's (``presentation_sharing.apply_sharing``):
    grants to existing accounts and groups, by tier; making it a company
    dashboard is an admin's act. The spec and the revision are untouched.
    """
    from app.services.presentation_sharing import apply_sharing

    dashboard, tier = await _load(db, user, dashboard_id, need="edit", for_update=True)
    await apply_sharing(db, user, dashboard, body)
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Changed who can open dashboard '{dashboard.title}'",
        details={
            "users": len(dashboard.shared_user_ids or []),
            "groups": len(dashboard.shared_group_ids or []),
            "is_company_managed": bool(dashboard.is_company_managed),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(dashboard)
    # Recomputed: making it a company dashboard may change the caller's own tier.
    found = await load_dashboard_for(db, dashboard.id, user_id=user.id, is_admin=user.is_admin)
    return _response(dashboard, found[1] if found else tier)


@router.get("/{dashboard_id}/versions", response_model=list[CustomDashboardVersionResponse])
async def list_custom_dashboard_versions(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The kept versions of the dashboard, newest first (whoever may read it)."""
    from app.services.presentation_versions import resolve_user_names

    dashboard, _tier = await _load(db, user, dashboard_id)
    versions = await list_dashboard_versions(db, dashboard.id)
    names = await resolve_user_names(db, [v.author_id for v in versions])
    return [
        CustomDashboardVersionResponse(
            **serialize_version(v), author_name=names.get(str(v.author_id)) if v.author_id else None
        )
        for v in versions
    ]


@router.get("/{dashboard_id}/versions/diff", response_model=DashboardVersionDiffResponse)
async def diff_custom_dashboard_versions(
    dashboard_id: str,
    from_revision: int = Query(..., alias="from", ge=1),
    to_revision: int = Query(..., alias="to", ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Two kept versions compared (whoever may read the dashboard); declared before ``/versions/{revision}``."""
    await enforce_rate_limit(None, "dashboard-version-diff", limit=30, window_seconds=60, bucket=str(user.id))
    dashboard, _tier = await _load(db, user, dashboard_id)
    diff = await diff_dashboard_versions(db, dashboard.id, from_revision, to_revision)
    if diff is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return DashboardVersionDiffResponse(**diff)


@router.get("/{dashboard_id}/versions/{revision}", response_model=CustomDashboardVersionResponse)
async def get_custom_dashboard_version(
    dashboard_id: str,
    revision: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One kept version with its spec, to preview it."""
    dashboard, _tier = await _load(db, user, dashboard_id)
    version = await get_dashboard_version(db, dashboard.id, revision)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return CustomDashboardVersionResponse(**serialize_version(version, include_spec=True))


@router.post("/{dashboard_id}/versions/{revision}/restore", response_model=CustomDashboardResponse)
async def restore_custom_dashboard_version(
    dashboard_id: str,
    revision: int,
    body: DashboardRestoreRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bring a kept version back as a new revision, at the revision the client read."""
    dashboard, tier = await _load(db, user, dashboard_id, need="edit", for_update=True)
    try:
        restored = await restore_dashboard_version(
            db, dashboard, revision, author_id=user.id, expected_revision=body.revision
        )
    except DashboardRevisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DashboardSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if restored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Restored revision {revision} of dashboard '{dashboard.title}'",
        details={"revision": dashboard.revision, "restored_from_revision": revision},
        request=request,
    )
    await db.commit()
    await db.refresh(dashboard)
    return _response(dashboard, tier)


@router.get("/{dashboard_id}/export")
async def export_custom_dashboard(
    dashboard_id: str,
    format: Literal["opencti", "opencti-custom-view", "openaev", "xtm"] = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The file OpenCTI or OpenAEV imports (or an XTM One widget pack), for whoever reads the dashboard.

    Only what the format holds is written; ``X-Left-Out`` counts the notes of what was left out or changed.
    The OpenCTI saved filters its widgets name are read with the caller's own access and written in place.
    """
    from app.services.dashboard_publish import PublishError, export_dashboard_file

    dashboard, _tier = await _load(db, user, dashboard_id)
    try:
        export = await export_dashboard_file(db, user.id, dashboard, format)
    except PublishError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    ascii_name = export.filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"', "X-Left-Out": str(len(export.notes))},
    )


@router.post("/{dashboard_id}/publish")
async def publish_custom_dashboard(
    dashboard_id: str,
    body: DashboardPublishRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish the dashboard to OpenCTI or OpenAEV as one of theirs, with the caller's own access there.

    Whoever reads the dashboard may publish it where their own account may
    create dashboards; the widgets the platform cannot hold are listed.
    """
    from app.services.dashboard_publish import PublishError, publish_dashboard, serialize_publication

    dashboard, _tier = await _load(db, user, dashboard_id)
    try:
        record, notes = await publish_dashboard(
            db, user_id=user.id, dashboard=dashboard, target=body.target,
            integration_id=body.integration_id, name=body.name,
        )  # fmt: skip
    except PublishError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await log_audit(
        db, user=user, action="update", entity_type="custom_dashboard", entity_id=dashboard.id,
        entity_name=dashboard.title, summary=f"Published dashboard '{dashboard.title}' to {body.target}",
        details={"target": body.target, "integration_id": str(record.integration_id), "revision": record.revision,
                 "left_out": len(notes)},
        request=request,
    )  # fmt: skip
    await db.commit()
    return {"publication": serialize_publication(record), "notes": notes}


@router.post("/{dashboard_id}/share-on-hub")
async def share_custom_dashboard_on_hub(
    dashboard_id: str,
    body: DashboardHubShareRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Share the dashboard on the XTM Hub: its OpenCTI export, as a new resource of a dashboards library.

    Whoever reads the dashboard, through an XTM Hub integration they can use,
    with their own Hub account (its library upload right). The resource starts
    inactive: a person makes it visible on the XTM Hub.
    """
    from app.services.dashboard_publish import PublishError, serialize_publication, share_on_hub

    dashboard, _tier = await _load(db, user, dashboard_id)
    try:
        record, notes = await share_on_hub(
            db, user_id=user.id, dashboard=dashboard, integration_id=body.integration_id, name=body.name,
            short_description=body.short_description, description=body.description,
        )  # fmt: skip
    except PublishError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await log_audit(
        db, user=user, action="update", entity_type="custom_dashboard", entity_id=dashboard.id,
        entity_name=dashboard.title, summary=f"Shared dashboard '{dashboard.title}' on the XTM Hub",
        details={"integration_id": str(record.integration_id), "revision": record.revision, "left_out": len(notes)},
        request=request,
    )  # fmt: skip
    await db.commit()
    return {"publication": serialize_publication(record), "notes": notes}


@router.get("/{dashboard_id}/publications")
async def list_custom_dashboard_publications(
    dashboard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Where the dashboard was published, most recent first."""
    from sqlalchemy import select

    from app.models.custom_dashboard import CustomDashboardPublication
    from app.services.dashboard_publish import serialize_publication

    dashboard, _tier = await _load(db, user, dashboard_id)
    rows = (
        await db.execute(
            select(CustomDashboardPublication)
            .where(CustomDashboardPublication.dashboard_id == dashboard.id)
            .order_by(CustomDashboardPublication.published_at.desc())
            .limit(50)
        )
    ).scalars()
    return [serialize_publication(row) for row in rows]


@router.put("/{dashboard_id}/tags", response_model=CustomDashboardResponse)
async def set_custom_dashboard_tags(
    dashboard_id: str,
    body: CustomDashboardTagsUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """File the dashboard under these tags (those who may edit it); its definition and revision are untouched."""
    dashboard, tier = await _load(db, user, dashboard_id, need="edit", for_update=True)
    dashboard.tags = clean_tags(body.tags)
    await log_audit(
        db, user=user, action="update", entity_type="custom_dashboard", entity_id=dashboard.id, entity_name=dashboard.title,
        summary=f"Tagged dashboard '{dashboard.title}'", details={"tags": dashboard.tags}, request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(dashboard)
    return _response(dashboard, tier)


@router.post("/{dashboard_id}/duplicate", response_model=CustomDashboardResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_custom_dashboard(
    dashboard_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """A private copy of a dashboard the caller may read; its platform widgets only through integrations they can use."""
    dashboard, _tier = await _load(db, user, dashboard_id)
    try:
        copy = await duplicate_dashboard(db, dashboard, user_id=user.id)
    except DashboardSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await log_audit(
        db, user=user, action="create", entity_type="custom_dashboard", entity_id=copy.id, entity_name=copy.title,
        summary=f"Duplicated dashboard '{dashboard.title}'", details={"from": str(dashboard.id)}, request=request,
    )  # fmt: skip
    await db.commit()
    await db.refresh(copy)
    return _response(copy, "owner")


@router.delete("/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_dashboard(
    dashboard_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a dashboard (its owner or a ``manage`` grantee; an admin, a company one). A chat marker then shows it as gone."""
    dashboard, tier = await _load(db, user, dashboard_id)
    if tier not in DELETE_TIERS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only its owner or a manager can delete a dashboard")
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="custom_dashboard",
        entity_id=dashboard.id,
        entity_name=dashboard.title,
        summary=f"Deleted dashboard '{dashboard.title}'",
        details={"widget_count": dashboard.widget_count, "revision": dashboard.revision},
        request=request,
    )
    from app.services.dashboard_assets import delete_dashboard_assets
    from app.services.file_cleanup import purge_storage_objects

    keys = await delete_dashboard_assets(db, dashboard.id)
    await db.delete(dashboard)
    await db.commit()
    if keys:
        await purge_storage_objects(keys)



# ── Export, and the render routes its page reads through (render token only) ──
#
# ``POST /{id}/render-export`` renders the dashboard in the export's headless
# browser (``dashboard_export``). The page it loads holds no session: the
# routes below accept only a render token bound to THIS dashboard, and apply
# the ordinary read rule to the token's user - the person who asked for the
# export reads every source with their own access, as on their screen.

RENDER_TOKEN_QUERY_PARAM = "render_token"  # noqa: S105 - a parameter name, not a secret

#: What a caller is told when a render fails: the reason goes to the logs only.
_RENDER_FAILED = "The dashboard could not be rendered."


async def _render_user(dashboard_id: str, request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """The user a dashboard render token stands for: 401 without a valid one, 403 when bound to another dashboard."""
    from app.core.utils import safe_uuid
    from app.services.dashboard_export import decode_dashboard_render_token
    from app.services.presentation_export import RenderTokenError

    token = request.query_params.get(RENDER_TOKEN_QUERY_PARAM, "")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Render token required")
    try:
        claims = decode_dashboard_render_token(token)
    except RenderTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token") from exc
    if safe_uuid(dashboard_id) != claims.dashboard_id:
        # A token is a capability over ONE dashboard.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    user = await db.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token")
    return user


@router.get("/{dashboard_id}/render", response_model=CustomDashboardResponse)
async def read_dashboard_for_render(
    dashboard_id: str,
    user: User = Depends(_render_user),
    db: AsyncSession = Depends(get_db),
):
    """The dashboard as the static render page draws it (render token only)."""
    dashboard, tier = await _load(db, user, dashboard_id)
    return _response(dashboard, tier)


@router.post(
    "/{dashboard_id}/render/widgets/{widget_id}/data",
    response_model=WidgetDataResponse,
)
async def read_widget_data_for_render(
    dashboard_id: str,
    widget_id: str,
    body: WidgetDataRequest,
    user: User = Depends(_render_user),
    db: AsyncSession = Depends(get_db),
):
    """A visualization's data for the static render page (render token only)."""
    return await _widget_data(db, user, dashboard_id, widget_id, body)


@router.post(
    "/{dashboard_id}/render/data-sources/{source_id}/data",
    response_model=WidgetDataResponse,
)
async def read_data_source_for_render(
    dashboard_id: str,
    source_id: str,
    body: WidgetDataRequest,
    user: User = Depends(_render_user),
    db: AsyncSession = Depends(get_db),
):
    """A data source's data for the static render page (render token only)."""
    return await _data_source(db, user, dashboard_id, source_id, body)


@router.get("/{dashboard_id}/render/assets/{file_id}")
async def read_asset_for_render(
    dashboard_id: str,
    file_id: str,
    user: User = Depends(_render_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """A file a canvas of the dashboard draws, for the static render page (render token only)."""
    return await _asset(db, user, dashboard_id, file_id)


@router.post("/{dashboard_id}/render-export")
async def export_dashboard_render(
    dashboard_id: str,
    body: DashboardRenderExportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The dashboard as a PDF or a PNG, or one visualization as a PNG, read with the caller's own access.

    A download, not a stored file (the ``export_dashboard`` tool stores one for
    an agent to send). Counted against the caller's export budget, shared with
    decks and reports; no pooled connection is held while the render waits for
    its slot and runs.
    """
    from app.core.database import release_db_connection_before_loop
    from app.core.utils import build_content_disposition, format_exception_message
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_export import DashboardRenderError, export_dashboard
    from app.services.presentation_export import enforce_export_rate_limit

    await enforce_export_rate_limit(user.id)
    dashboard, _tier = await _load(db, user, dashboard_id)
    definition = upgrade(dashboard.spec)
    layouts = definition["layout"]["layoutDefinitions"]
    if body.layout_ids and any(lid not in layouts for lid in body.layout_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="No such tab")
    if body.visualization_id is not None and body.visualization_id not in (definition.get("visualizations") or {}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Widget not found")
    dashboard_uuid, title = dashboard.id, dashboard.title
    await release_db_connection_before_loop(db)
    try:
        exported = await export_dashboard(
            dashboard_id=dashboard_uuid,
            user_id=user.id,
            title=title,
            export_format=body.format,
            layout_ids=body.layout_ids,
            visualization_id=body.visualization_id,
        )
    except DashboardRenderError as exc:
        logger.warning("Dashboard export failed: dashboard=%s: %r", dashboard_uuid, str(exc)[:300])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_RENDER_FAILED) from exc
    except Exception as exc:
        logger.error("Dashboard export crashed: dashboard=%s: %s", dashboard_uuid, format_exception_message(exc), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_RENDER_FAILED) from exc
    return Response(
        content=exported.data,
        media_type=exported.content_type,
        headers={"Content-Disposition": build_content_disposition(exported.filename), "Cache-Control": "private, no-store"},
    )


# ── The published link (managers) ────────────────────────────────────────────
#
# A link anyone may open without an account, showing a snapshot of the data
# read with the publisher's access (``dashboard_public_links``). Managers only,
# the edit tiers (``EDIT_TIERS``): the owner, an admin on a company dashboard,
# a ``config`` or ``config_with_chats`` grantee; behind the admin switch
# ``dashboard_public_links_enabled`` for creating one.

#: Snapshots a manager may ask to read again, per person and minute.
PUBLIC_SNAPSHOT_REFRESHES_PER_MINUTE = 6


async def _public_link_response(db: AsyncSession, dashboard: CustomDashboard) -> DashboardPublicLinkResponse:
    from app.services.dashboard_public_links import (
        link_token,
        load_public_link,
        public_link_path,
        public_link_url,
        public_links_enabled,
    )

    enabled = public_links_enabled()
    link = await load_public_link(db, dashboard.id)
    if link is None:
        return DashboardPublicLinkResponse(enabled_on_platform=enabled)
    token = link_token(link)
    publisher = await db.get(User, link.created_by_user_id) if link.created_by_user_id else None
    return DashboardPublicLinkResponse(
        enabled_on_platform=enabled,
        url=public_link_url(token) if token else None,
        path=public_link_path(token) if token else None,
        created_at=link.created_at.isoformat() if link.created_at else None,
        publisher_name=publisher.full_name if publisher else None,
        refresh_minutes=link.refresh_minutes,
        snapshot_at=link.snapshot_at.isoformat() if link.snapshot_at else None,
        marking_ceiling=[str(m) for m in link.marking_ceiling or []],
    )


async def _set_link_options(
    db: AsyncSession,
    dashboard: CustomDashboard,
    user: User,
    *,
    refresh_minutes: int | None,
    marking_ceiling: list[str] | None,
) -> bool:
    """Set how often the link's snapshot is read again and its marking ceiling; ``False`` without a link.

    A new ceiling widens or narrows what the link shows, so it is never read
    with someone else's access: *user* becomes the publisher. The caller
    commits, then reads the snapshot again (``_read_under_new_ceiling``).
    """
    from app.services.dashboard_public_links import follow_author, set_link_options

    changed = await set_link_options(db, dashboard.id, refresh_minutes=refresh_minutes, marking_ceiling=marking_ceiling)
    if changed is None:
        return False
    if marking_ceiling is not None:
        await follow_author(db, dashboard.id, user.id)
    return True


async def _read_under_new_ceiling(db: AsyncSession, dashboard: CustomDashboard) -> None:
    """Read the snapshot again now, under the link's new ceiling, with its publisher's access."""
    from app.services.dashboard_public_links import current_snapshot, load_public_link, resolve_publisher

    link = await load_public_link(db, dashboard.id)
    publisher = await resolve_publisher(db, dashboard, link) if link is not None else None
    if link is not None and publisher is not None:
        await current_snapshot(db, dashboard, link, publisher, force=True)


async def _take_snapshot(db: AsyncSession, dashboard: CustomDashboard, user: User) -> None:
    """Read the snapshot now, as *user* (the publisher); the link answers for a publisher who may read the dashboard."""
    from app.services.dashboard_public_links import current_snapshot, load_public_link

    link = await load_public_link(db, dashboard.id)
    if link is not None:
        await current_snapshot(db, dashboard, link, user, force=True)


@router.get("/{dashboard_id}/public-link", response_model=DashboardPublicLinkResponse)
async def read_dashboard_public_link(
    dashboard_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """The dashboard's published link, for its managers."""
    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    return await _public_link_response(db, dashboard)


@router.post("/{dashboard_id}/public-link", response_model=DashboardPublicLinkResponse)
async def publish_dashboard_link(
    dashboard_id: str,
    body: DashboardPublicLinkRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish the dashboard as a link (its first snapshot read now, with the caller's access), or regenerate it.

    On a dashboard that already has a link, ``refresh_minutes`` and
    ``marking_ceiling`` apply to it, as ``PATCH`` does: the answer never
    carries settings other than the ones asked for.
    """
    from app.services.dashboard_public_links import (
        OUTCOME_KEPT,
        PUBLIC_LINKS_OFF_DETAIL,
        audit_details,
        enable_public_link,
        public_links_enabled,
    )

    if not public_links_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PUBLIC_LINKS_OFF_DETAIL)
    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    change = await enable_public_link(
        db,
        dashboard.id,
        actor_id=user.id,
        regenerate=body.regenerate,
        refresh_minutes=body.refresh_minutes,
        marking_ceiling=body.marking_ceiling,
    )
    # Already published: the settings sent apply to the link kept, as PATCH does.
    apply_options = change.outcome == OUTCOME_KEPT and (body.refresh_minutes is not None or body.marking_ceiling is not None)
    if apply_options:
        await _set_link_options(
            db, dashboard, user, refresh_minutes=body.refresh_minutes, marking_ceiling=body.marking_ceiling
        )
    if change.changed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="custom_dashboard",
            entity_id=dashboard.id,
            entity_name=dashboard.title,
            summary=f"Published link of dashboard '{dashboard.title}' {change.outcome}",
            details=audit_details(change.outcome),
            request=request,
        )
    await db.commit()
    if change.changed:
        await _take_snapshot(db, dashboard, user)
    elif apply_options and body.marking_ceiling is not None:
        await _read_under_new_ceiling(db, dashboard)
    return await _public_link_response(db, dashboard)


@router.patch("/{dashboard_id}/public-link", response_model=DashboardPublicLinkResponse)
async def update_dashboard_public_link(
    dashboard_id: str,
    body: DashboardPublicLinkUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """How often the published link's snapshot is read again, and its marking ceiling (a new one reads it again,
    with the access of whoever set it: they become the link's publisher)."""
    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    if not await _set_link_options(
        db, dashboard, user, refresh_minutes=body.refresh_minutes, marking_ceiling=body.marking_ceiling
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This dashboard has no published link")
    await db.commit()
    if body.marking_ceiling is not None:
        await _read_under_new_ceiling(db, dashboard)
    return await _public_link_response(db, dashboard)


@router.post(
    "/{dashboard_id}/public-link/refresh", response_model=DashboardPublicLinkResponse
)
async def refresh_dashboard_public_link(
    dashboard_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Read the published link's snapshot again now, with its publisher's access."""
    from app.services.dashboard_public_links import current_snapshot, load_public_link, resolve_publisher

    await enforce_rate_limit(
        None, "dashboard-public-refresh", limit=PUBLIC_SNAPSHOT_REFRESHES_PER_MINUTE, window_seconds=60, bucket=str(user.id)
    )
    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    link = await load_public_link(db, dashboard.id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This dashboard has no published link")
    publisher = await resolve_publisher(db, dashboard, link)
    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Its publisher can no longer open this dashboard: publish it again to read it with your access.",
        )
    await current_snapshot(db, dashboard, link, publisher, force=True)
    return await _public_link_response(db, dashboard)


@router.delete("/{dashboard_id}/public-link", status_code=status.HTTP_204_NO_CONTENT)
async def unpublish_dashboard_link(
    dashboard_id: str, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    """Turn the published link off: its address stops answering at once (never refused, the switch off included)."""
    from app.services.dashboard_public_links import OUTCOME_DISABLED, audit_details, disable_public_link

    dashboard, _tier = await _load(db, user, dashboard_id, need="edit")
    if await disable_public_link(db, dashboard.id):
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="custom_dashboard",
            entity_id=dashboard.id,
            entity_name=dashboard.title,
            summary=f"Published link of dashboard '{dashboard.title}' turned off",
            details=audit_details(OUTCOME_DISABLED),
            request=request,
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
