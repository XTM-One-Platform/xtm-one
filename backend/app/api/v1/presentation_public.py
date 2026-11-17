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

"""What a presentation's public link serves, without an account.

The only presentation routes with no authentication: the token in the path
IS the credential (``services/presentation_public_links``). Mounted under
``/api/v1/public`` beside the chat widget, so the public CORS layer applies,
and bounded like every unauthenticated surface:

* **One answer for every refusal.** An unknown token, a regenerated or
  disabled one, a deleted deck and the admin switch off are the same 404, so
  the answer never tells a dead link from one that never existed. A file the
  deck does not reference is the unknown-file 404.
* **Never cached** (``Cache-Control: no-store``): a link turned off must stop
  answering at once, including from a shared cache, and the bytes of a
  picture are read as they are now. ``Referrer-Policy: no-referrer``
  keeps the token out of whatever a slide links to.
* **Rate limited per client address**, generously: a room of people behind
  one address opening the link together, each page fetching every picture
  of the deck, must not be refused, while one client is still bounded.
* **The PDF** is offered only when the link's managers allowed it,
  and it is the expensive answer: cached per deck content, renders budgeted
  per link, run in the worker's one public render slot
  (``presentation_public_links.public_pdf``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import parse_path_id
from app.api.v1.schemas.presentation import PublicPresentationResponse
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.utils import build_content_disposition, format_exception_message
from app.services.presentation_export import PDF_CONTENT_TYPE, PresentationRenderError
from app.services.presentation_public_links import (
    PDF_DOWNLOAD_OFF_DETAIL,
    PUBLIC_PDF_DOWNLOADS_PER_ADDRESS,
    PUBLIC_PDF_WINDOW_SECONDS,
    public_asset_ids,
    public_links_enabled,
    public_pdf,
    public_presentation_payload,
    resolve_public_link,
    resolve_public_presentation,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: What a visitor is told when the PDF cannot be produced; the operator
#: detail (headless browser, render URL) goes to the log only.
PDF_UNAVAILABLE_DETAIL = "The PDF could not be generated right now. Try again in a few minutes."

#: Deck reads per client address per window: a room opening the link at once.
PUBLIC_DECK_RATE_LIMIT = 300
#: Picture reads per client address per window: that room, every picture each.
PUBLIC_ASSET_RATE_LIMIT = 3000
RATE_WINDOW_SECONDS = 60

#: On every answer, the 404s included.
PUBLIC_HEADERS: dict[str, str] = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

NOT_FOUND_DETAIL = "This presentation is not available"


def _not_found(detail: str = NOT_FOUND_DETAIL) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail, headers=PUBLIC_HEADERS)


@router.get("/presentations/{token}", response_model=PublicPresentationResponse)
async def get_public_presentation(
    token: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """The deck a public link opens: its slides as the audience sees them, without speaker notes."""
    await enforce_rate_limit(
        request, "presentation_public", limit=PUBLIC_DECK_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS
    )
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    presentation, link = resolved
    response.headers.update(PUBLIC_HEADERS)
    return PublicPresentationResponse(**await public_presentation_payload(db, presentation, link))


@router.get("/presentations/{token}/pdf")
async def get_public_presentation_pdf(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """The PDF of the deck as the link shows it, iff the link offers a download.

    Rendered from the public page itself, so it carries the slides and never
    a speaker note; served from this worker's cache for an unchanged deck.
    Downloads are budgeted per client address and renders per link (429);
    a deployment that cannot render answers 503 without its operator detail.
    """
    await enforce_rate_limit(
        request,
        "presentation_public_pdf",
        limit=PUBLIC_PDF_DOWNLOADS_PER_ADDRESS,
        window_seconds=PUBLIC_PDF_WINDOW_SECONDS,
    )
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    presentation, link = resolved
    if not link.allow_pdf_download:
        raise _not_found(PDF_DOWNLOAD_OFF_DETAIL)
    deck_id = presentation.id
    try:
        pdf = await public_pdf(db, presentation, link, token)
    except HTTPException:
        raise
    except PresentationRenderError as exc:
        logger.warning("Public presentation PDF failed: deck=%s: %s", deck_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=PDF_UNAVAILABLE_DETAIL, headers=PUBLIC_HEADERS
        ) from exc
    except Exception as exc:
        logger.error(
            "Public presentation PDF crashed: deck=%s: %s", deck_id, format_exception_message(exc), exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PDF_UNAVAILABLE_DETAIL, headers=PUBLIC_HEADERS
        ) from exc
    return Response(
        content=pdf.data,
        media_type=PDF_CONTENT_TYPE,
        headers={
            **PUBLIC_HEADERS,
            "Content-Disposition": build_content_disposition(pdf.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/presentations/{token}/assets/{file_id}")
async def get_public_presentation_asset(
    token: str,
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """One picture or logo the deck shows, iff the deck references it."""
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import FILE_NOT_FOUND_DETAIL
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    await enforce_rate_limit(
        request, "presentation_public_asset", limit=PUBLIC_ASSET_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS
    )
    presentation = await resolve_public_presentation(db, token)
    if presentation is None:
        raise _not_found()
    asset_id = parse_path_id(file_id, not_found_detail=FILE_NOT_FOUND_DETAIL)
    if str(asset_id) not in await public_asset_ids(db, presentation):
        raise _not_found(FILE_NOT_FOUND_DETAIL)
    file_att = (await db.execute(select(FileAttachment).where(FileAttachment.id == asset_id))).scalar_one_or_none()
    if file_att is None:
        raise _not_found(FILE_NOT_FOUND_DETAIL)
    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        raise _not_found("File no longer available") from None
    return Response(
        content=data,
        media_type=file_att.content_type,
        headers={**PUBLIC_HEADERS, **file_response_headers(file_att, store=False)},
    )


# ── Live poll audience (xtm-one #10157) ────────────────────────────────────────
# ``/v/CODE`` reads one poll by its short code and votes on its open question;
# nothing else of the deck. Off with the deck public links (one admin switch
# for "a deck answers someone without an account"). A room behind one address
# polls every 2 s per phone, hence the per-address budgets.

POLL_READ_RATE_LIMIT = 6000
POLL_VOTE_RATE_LIMIT = 1200
POLL_NOT_FOUND_DETAIL = "This poll is not available"


class PollVoteRequest(BaseModel):
    question_id: str = Field(..., max_length=32)
    voter: str = Field(..., min_length=16, max_length=128, description="the random token the page keeps")
    answer: int | str = Field(..., description="option index, rating 1-5, or the words of a word cloud")


async def _poll_context(db: AsyncSession, code: str):
    from app.services.presentation_polls import resolve_code

    if not public_links_enabled():
        raise _not_found(POLL_NOT_FOUND_DETAIL)
    ctx = await resolve_code(db, code)
    if ctx is None:
        raise _not_found(POLL_NOT_FOUND_DETAIL)
    return ctx


@router.get("/polls/{code}")
async def get_public_poll(code: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """The open question of one poll (and its results when the presenter shows them)."""
    from app.services.presentation_polls import poll_state

    await enforce_rate_limit(
        request, "presentation_poll_read", limit=POLL_READ_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS
    )
    ctx = await _poll_context(db, code)
    response.headers.update(PUBLIC_HEADERS)
    return await poll_state(db, ctx.poll, ctx.block, audience=True)


@router.post("/polls/{code}/votes")
async def vote_public_poll(
    code: str, body: PollVoteRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    """One anonymous answer; a second answer from the same device to the same question is ignored."""
    from app.services.presentation_polls import PollError, cast_vote

    await enforce_rate_limit(
        request, "presentation_poll_vote", limit=POLL_VOTE_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS
    )
    ctx = await _poll_context(db, code)
    try:
        recorded = await cast_vote(db, ctx, question_key=body.question_id, voter_token=body.voter, answer=body.answer)
    except PollError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": exc.reason, "message": str(exc)},
            headers=PUBLIC_HEADERS,
        ) from exc
    await db.commit()
    response.headers.update(PUBLIC_HEADERS)
    return {"recorded": recorded, "already_voted": not recorded}
