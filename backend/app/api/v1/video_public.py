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

"""What a video's public link serves, without an account.

The only Video Studio routes with no authentication: the token in the path IS
the credential (``services/video_studio/public_links``). Mounted under
``/api/v1/public`` beside the public decks, so the public CORS layer applies,
and bounded like every unauthenticated surface:

* **One answer for every refusal.** An unknown token, a regenerated or
  disabled one, a deleted video and the admin switch off are the same 404, so
  the answer never tells a dead link from one that never existed. A file is
  served only when it is one of the CURRENT render's (its MP4, poster or
  captions), never by id.
* **Never cached** (``Cache-Control: no-store``): a link turned off must stop
  playing at once, including from a shared cache. ``Referrer-Policy:
  no-referrer`` keeps the token out of whatever the page links to.
* **Rate limited per client address**, generously for the media: a player
  seeks by byte ranges, a few megabytes each, and a room watching together
  sits behind one address. The media also have a budget of BYTES per address
  (:data:`PUBLIC_MEDIA_BYTES_PER_WINDOW`): each answer is charged what
  it carries - a range, or the whole file - and the one that would cross it is
  a 429, so one address can never pull more than that per window.
* **The download** is an attachment named after the video, offered only when
  the link's managers allowed it. It is a convenience, not a protection:
  anyone who can watch the video can save the bytes the player plays.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.video_studio import PublicVideoResponse
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.utils import build_content_disposition
from app.services.video_studio import media
from app.services.video_studio.public_links import (
    DOWNLOADABLE_ASSETS,
    PUBLIC_ASSETS,
    current_render,
    public_file_id,
    public_video_payload,
    resolve_public_link,
)

router = APIRouter()

#: Video reads per client address per window: a room opening the link at once.
PUBLIC_VIDEO_RATE_LIMIT = 300
#: Media reads per client address per window: every viewer's byte ranges.
PUBLIC_MEDIA_RATE_LIMIT = 6000
#: Bytes of media per client address per window: some thirty viewers of a 1080p60 render behind one
#: address, where the request limit alone let one address pull 6000 answers of 4 MiB - about 24 GB - a minute.
PUBLIC_MEDIA_BYTES_PER_WINDOW = 2 * 1024**3
RATE_WINDOW_SECONDS = 60
#: Downloads per client address per window: the attachment, not the playing.
PUBLIC_DOWNLOADS_PER_ADDRESS = 60
DOWNLOAD_WINDOW_SECONDS = 600

#: On every answer, the 404s included.
PUBLIC_HEADERS: dict[str, str] = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

NOT_FOUND_DETAIL = "This video is not available"
DOWNLOAD_OFF_DETAIL = "This video cannot be downloaded"


def _not_found(detail: str = NOT_FOUND_DETAIL) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail, headers=PUBLIC_HEADERS)


@router.get("/videos/{token}", response_model=PublicVideoResponse)
async def get_public_video(token: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """The video a public link opens: its title and its current render, as the player needs them."""
    await enforce_rate_limit(request, "video_public", limit=PUBLIC_VIDEO_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    project, link = resolved
    response.headers.update(PUBLIC_HEADERS)
    return PublicVideoResponse(**public_video_payload(project, await current_render(db, project), link))


@router.get("/videos/{token}/media/{asset}")
async def get_public_video_media(
    token: str,
    asset: str,
    request: Request,
    download: bool = False,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """One file of the current render (``video``, ``poster``, ``captions``), by byte ranges.

    ``?download=true`` answers it as an attachment named after the video,
    only for the MP4 and its captions and only when the link allows it.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import FileNotFoundInStorageError

    await enforce_rate_limit(
        request, "video_public_media", limit=PUBLIC_MEDIA_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS
    )
    if asset not in PUBLIC_ASSETS:
        raise _not_found()
    resolved = await resolve_public_link(db, token)
    if resolved is None:
        raise _not_found()
    project, link = resolved
    if download:
        if asset not in DOWNLOADABLE_ASSETS or not link.allow_download:
            raise _not_found(DOWNLOAD_OFF_DETAIL)
        await enforce_rate_limit(
            request,
            "video_public_download",
            limit=PUBLIC_DOWNLOADS_PER_ADDRESS,
            window_seconds=DOWNLOAD_WINDOW_SECONDS,
        )
    render = await current_render(db, project)
    file_id = public_file_id(render, asset)
    row = await db.get(FileAttachment, file_id) if file_id else None
    if row is None or render is None:
        raise _not_found()
    range_header = request.headers.get("range")
    await enforce_rate_limit(
        request,
        "video_public_media_bytes",
        limit=PUBLIC_MEDIA_BYTES_PER_WINDOW,
        window_seconds=RATE_WINDOW_SECONDS,
        cost=max(1, media.answer_bytes(range_header, int(row.size or 0))),
    )
    disposition = (
        build_content_disposition(media.download_filename(project.title, asset, height=render.height, fps=render.fps))
        if download
        else "inline"
    )
    headers = {
        **PUBLIC_HEADERS,
        "Content-Disposition": disposition,
        "Content-Type": row.content_type or "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        return await media.serve_file(row, range_header, headers)
    except FileNotFoundInStorageError:
        raise _not_found() from None
