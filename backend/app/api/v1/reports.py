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

"""Report API — read access to agent-generated executive reports.

The reports themselves are written by the ``generate_report`` built-in tool, never
through this surface: an agent produces a validated ``ReportSpec``, and the
frontend fetches it here to render the document behind a ``[[REPORT:<id>]]`` chat
marker (or on the standalone ``/reports/:id`` viewer). There is deliberately no
create/update endpoint: the spec is only ever written through the validated tool
path, so the UI can never introduce a report shape the validator would reject.
``POST /reports/{id}/export`` writes nothing either: it renders the report to a
PDF server-side (``services/report_export.py``) and streams it back.

Access is owner-or-admin, mirroring ``GET /presentations/{id}``: a report can
embed text from any context the agent had, so it is as private as the
conversation that produced it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import parse_path_id, parse_uuid
from app.api.v1.schemas.report import ReportResponse, ReportSummaryResponse
from app.core.database import get_db, release_db_connection_before_loop
from app.core.utils import build_content_disposition, format_exception_message, safe_uuid
from app.models.report import Report
from app.models.user import User
from app.services.presentation_export import RenderTokenError, enforce_export_rate_limit
from app.services.report_export import (
    ReportRenderClaims,
    ReportRenderError,
    decode_report_render_token,
    export_report,
)
from app.services.reports import resolve_report_theme, serialize_report

logger = logging.getLogger(__name__)
router = APIRouter()

#: Cap on the list endpoint. A report list is a navigation aid, not a dataset.
MAX_LIST_RESULTS = 100

#: Query parameter the static render page passes its render token in.
RENDER_TOKEN_QUERY_PARAM = "render_token"  # noqa: S105 - a parameter name, not a secret

EXPORT_UNAVAILABLE_DETAIL = (
    "Report export is unavailable: the platform's headless browser could not "
    "render the report. Check PRESENTATION_RENDER_BASE_URL and that Chromium "
    "is installed for Playwright."
)


def _assert_readable(report: Report, user: User) -> None:
    """Owner-or-admin gate (same contract as ``GET /presentations/{id}``)."""
    if report.user_id and report.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )
    if report.user_id is None and user.role != "admin":
        # Owner deleted: keep the artifact readable for auditing, admins only.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )


@router.get("", response_model=list[ReportSummaryResponse])
async def list_reports(
    conversation_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_RESULTS),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's reports, most recently updated first."""
    query = select(Report).where(Report.user_id == user.id)
    if conversation_id:
        query = query.where(
            Report.conversation_id
            == parse_uuid(conversation_id, label="conversation ID")
        )
    result = await db.execute(query.order_by(Report.updated_at.desc()).limit(limit))
    return [
        ReportSummaryResponse(
            id=str(r.id),
            title=r.title,
            theme=r.theme,
            block_count=r.block_count,
            revision=r.revision,
            conversation_id=str(r.conversation_id) if r.conversation_id else None,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )
        for r in result.scalars().all()
    ]


async def _load_readable(db: AsyncSession, user: User, report_id: str | uuid.UUID) -> Report:
    """The report *report_id* names, if *user* may read it (404 / 403 otherwise)."""
    doc_id = parse_path_id(report_id, not_found_detail="Report not found")
    result = await db.execute(select(Report).where(Report.id == doc_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
        )
    _assert_readable(report, user)
    return report


async def _report_response(db: AsyncSession, report: Report, user: User) -> ReportResponse:
    # Palette overrides are resolved against the *reader's* visibility: a template
    # that has since been un-shared contributes nothing rather than leaking its
    # brand config (the report still renders with its built-in theme). A report
    # that links no template wears the platform's brand, resolved here at read
    # time so the viewer, the static render and the PDF export agree.
    theme_config = await resolve_report_theme(
        db, report.template_container_id, user_id=user.id, report_theme=report.theme
    )
    return ReportResponse(**serialize_report(report, theme_config=theme_config))


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one report's spec plus its resolved template palette overrides."""
    report = await _load_readable(db, user, report_id)
    return await _report_response(db, report, user)


@router.post("/{report_id}/export")
async def export_report_endpoint(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the report server-side and return it as a PDF download.

    Same access as reading the report. The bytes are streamed back rather than
    stored: an interactive export is a download, not an artifact of the
    conversation (the ``export_report`` tool is the path that persists one as a
    ``FileAttachment`` the agent can hand over). Counted against the user's
    export budget, shared with the deck export, and no pooled connection is
    held while the render waits for its slot and runs.
    """
    await enforce_export_rate_limit(user.id)
    report = await _load_readable(db, user, report_id)
    await release_db_connection_before_loop(db)
    try:
        exported = await export_report(report_id=report.id, user_id=user.id, title=report.title)
    except ReportRenderError as exc:
        logger.warning("Report export failed: report=%s: %s", report.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "Report export crashed: report=%s: %s",
            report.id,
            format_exception_message(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=EXPORT_UNAVAILABLE_DETAIL,
        ) from exc
    # ROI of chat work (#10544, S2b): an export is a use of the report.
    from app.services.roi_chat_records import record_signal_detached

    await record_signal_detached(kind="report", deliverable_id=report.id, signal="exported")
    return Response(
        content=exported.data,
        media_type=exported.content_type,
        headers={
            "Content-Disposition": build_content_disposition(exported.filename),
            "Cache-Control": "private, no-store",
        },
    )


# ── Static render route — render-token only ──────────────────────────
#
# What the headless browser behind the export calls. It accepts ONLY a report
# render token (a confined JWT bound to one report, see
# ``report_export.mint_report_render_token``); a session JWT is refused here just
# as a render token is refused by ``get_current_user``.


def _render_claims(request: Request, report_id: str) -> ReportRenderClaims:
    token = request.query_params.get(RENDER_TOKEN_QUERY_PARAM, "")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Render token required")
    try:
        claims = decode_report_render_token(token)
    except RenderTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token") from exc
    doc_id = safe_uuid(report_id)
    if doc_id is None or claims.report_id != doc_id:
        # Bound to another report: a token is a capability over ONE report.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return claims


@router.get("/{report_id}/render-spec", response_model=ReportResponse)
async def render_spec(
    report_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """The report as the static render page consumes it (render token only)."""
    claims = _render_claims(request, report_id)
    user = (await db.execute(select(User).where(User.id == claims.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token")
    report = await _load_readable(db, user, claims.report_id)
    return await _report_response(db, report, user)
