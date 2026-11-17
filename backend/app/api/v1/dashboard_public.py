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

"""Published custom dashboards - no account (``/api/v1/public/dashboards/{token}``).

A published link (``services/dashboard_public_links.py``) opens a dashboard
for anyone holding its URL: its definition reduced to what draws and the
snapshot of its data read with the publisher's access, never live with a
visitor's. Every refusal - the switch off, a malformed, unknown, regenerated
or turned off token, a publisher who may no longer read the dashboard, a file
the dashboard does not draw - is the same 404. Reads are budgeted per client
address and refused when the budget cannot be checked (fail-closed); no
answer is cached on the way and none sends a referrer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.services.dashboard_public_links import public_payload, resolve_public_link

logger = logging.getLogger(__name__)
router = APIRouter()

#: Dashboard reads per client address per window: a room opening the link at once.
PUBLIC_DASHBOARD_RATE_LIMIT = 300
#: File reads per client address per window: every image of every visit.
PUBLIC_ASSET_RATE_LIMIT = 3000
RATE_WINDOW_SECONDS = 60

#: On every answer, the 404s included.
PUBLIC_HEADERS: dict[str, str] = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

NOT_FOUND_DETAIL = "This dashboard is not available"


class PublicDashboardResponse(BaseModel):
    """What a published link shows: the title, the definition reduced to what draws, the snapshot and its time."""

    title: str
    definition: dict[str, Any]
    snapshot: dict[str, Any]
    snapshot_at: str | None = None


def _not_found(detail: str = NOT_FOUND_DETAIL) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail, headers=PUBLIC_HEADERS)


@router.get("/dashboards/{token}", response_model=PublicDashboardResponse)
async def get_public_dashboard(token: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """The dashboard a published link opens, with its snapshot (read again first when it is stale)."""
    await enforce_rate_limit(
        request, "dashboard_public", limit=PUBLIC_DASHBOARD_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS, fail_closed=True
    )
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    dashboard, link, publisher = resolved
    response.headers.update(PUBLIC_HEADERS)
    return PublicDashboardResponse(**await public_payload(db, dashboard, link, publisher))


@router.get("/dashboards/{token}/assets/{file_id}")
async def get_public_dashboard_asset(token: str, file_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """One file a canvas of the published dashboard draws, iff its definition draws it now."""
    from app.api.v1.custom_dashboards import ASSET_CSP
    from app.services.dashboard_assets import load_drawn_asset
    from app.services.dashboard_definition import upgrade
    from app.services.file_access import FILE_NOT_FOUND_DETAIL
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    await enforce_rate_limit(
        request, "dashboard_public_asset", limit=PUBLIC_ASSET_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS, fail_closed=True
    )
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    dashboard = resolved[0]
    row = await load_drawn_asset(db, dashboard.id, upgrade(dashboard.spec), file_id)
    if row is None:
        raise _not_found(FILE_NOT_FOUND_DETAIL)
    try:
        data = await file_storage.download(row.s3_object_name)
    except FileNotFoundInStorageError:
        raise _not_found(FILE_NOT_FOUND_DETAIL) from None
    headers = {**PUBLIC_HEADERS, **file_response_headers(row, store=False), "Content-Security-Policy": ASSET_CSP}
    return Response(content=data, media_type=row.content_type, headers=headers)
