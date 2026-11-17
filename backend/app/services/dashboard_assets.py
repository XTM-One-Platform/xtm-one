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

"""Files uploaded to a custom dashboard: the images, icons and backgrounds its canvases draw.

An asset is a ``FileAttachment`` of source ``dashboard_asset`` stored under
``dashboard-assets/<dashboard id>/``: the object key binds it to ONE dashboard,
so an editor of another dashboard naming its id in theirs reads nothing. It is
served by ``GET /custom-dashboards/{id}/assets/{file_id}`` to whoever reads that
dashboard, and only while its definition draws it (``dashboard_canvas.file_ids``),
like a deck serves the pictures its slides name.

What may be uploaded is closed: PNG, JPEG, GIF, WebP (their bytes checked, not
the declared type) and SVG, cleaned by ``svg_sanitizer`` on the way into
storage exactly like a template asset (``store_template_asset_bytes``' rule);
at most :data:`MAX_ASSET_BYTES` each and :data:`MAX_ASSETS` per dashboard.
Deleting the dashboard deletes its assets.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_attachment import FileAttachment

DASHBOARD_ASSET_SOURCE = "dashboard_asset"
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_ASSETS = 100

#: The raster types accepted, by the bytes they start with.
_RASTER_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}
SVG_TYPE = "image/svg+xml"


class DashboardAssetError(ValueError):
    """An upload refused; the message is safe to show (no path, no content)."""


def asset_prefix(dashboard_id: uuid.UUID) -> str:
    return f"dashboard-assets/{dashboard_id}"


def sniff_image_type(data: bytes) -> str | None:
    """The raster type the bytes ARE (``None`` for anything else, an SVG included)."""
    for content_type, magics in _RASTER_MAGIC.items():
        if data.startswith(magics):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_svg(content_type: str | None, filename: str) -> bool:
    from app.services.svg_sanitizer import looks_like_svg

    return looks_like_svg(content_type, filename)


async def count_assets(db: AsyncSession, dashboard_id: uuid.UUID) -> int:
    prefix = asset_prefix(dashboard_id) + "/"
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(FileAttachment)
                .where(
                    FileAttachment.source == DASHBOARD_ASSET_SOURCE,
                    FileAttachment.s3_object_name.startswith(prefix, autoescape=True),
                )
            )
        ).scalar_one()
    )


async def store_asset(
    db: AsyncSession,
    *,
    dashboard_id: uuid.UUID,
    user_id: uuid.UUID,
    data: bytes,
    filename: str,
    content_type: str | None,
) -> FileAttachment:
    """Check, clean and store one upload; the row is flushed, the caller commits.

    Raises :class:`DashboardAssetError` for an empty, too large, unknown or
    malformed file, or a dashboard already holding :data:`MAX_ASSETS`.
    """
    from app.services.file_storage import file_storage
    from app.services.svg_sanitizer import SvgSanitizeError, sanitize_svg

    if not data:
        raise DashboardAssetError("The file is empty.")
    if len(data) > MAX_ASSET_BYTES:
        raise DashboardAssetError(f"The file is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB.")
    if await count_assets(db, dashboard_id) >= MAX_ASSETS:
        raise DashboardAssetError(f"A dashboard holds at most {MAX_ASSETS} files. Remove one first.")
    name = (filename or "image").replace("\r", " ").replace("\n", " ")[:200]
    stored_type = sniff_image_type(data)
    if stored_type is None:
        if not _is_svg(content_type, name):
            raise DashboardAssetError("Only PNG, JPEG, GIF, WebP and SVG images can be added to a dashboard.")
        try:
            data = sanitize_svg(data)
        except SvgSanitizeError:
            raise DashboardAssetError("The SVG could not be read as an SVG document.") from None
        stored_type = SVG_TYPE
    key = await file_storage.upload(data=data, filename=name, content_type=stored_type, prefix=asset_prefix(dashboard_id))
    row = FileAttachment(
        user_id=user_id,
        filename=name,
        content_type=stored_type,
        size=len(data),
        s3_object_name=key,
        sha256=hashlib.sha256(data).hexdigest(),
        source=DASHBOARD_ASSET_SOURCE,
    )
    db.add(row)
    await db.flush()
    return row


async def load_drawn_asset(db: AsyncSession, dashboard_id: uuid.UUID, definition: dict, file_id: str) -> FileAttachment | None:
    """The asset *file_id* names iff it is this dashboard's and *definition* draws it; ``None`` otherwise."""
    from app.core.utils import safe_uuid
    from app.services.dashboard_canvas import file_ids

    file_uuid = safe_uuid(file_id)
    if file_uuid is None or str(file_uuid) not in file_ids(definition):
        return None
    row = (await db.execute(select(FileAttachment).where(FileAttachment.id == file_uuid))).scalar_one_or_none()
    if row is None or row.source != DASHBOARD_ASSET_SOURCE:
        return None
    if not (row.s3_object_name or "").startswith(asset_prefix(dashboard_id) + "/"):
        return None
    return row


async def copy_drawn_assets(
    db: AsyncSession, *, source_id: uuid.UUID, target_id: uuid.UUID, user_id: uuid.UUID, definition: dict
) -> dict[str, str]:
    """Copy the assets *definition* draws from dashboard *source_id* to *target_id* (a copy of it), as *user_id*'s;
    each old file id to its copy's. An asset the source no longer holds is left out (the copy draws nothing there).
    The rows are flushed; the caller commits."""
    from app.services.dashboard_canvas import file_ids
    from app.services.file_storage import file_storage

    copied: dict[str, str] = {}
    for fid in sorted(file_ids(definition)):
        row = await load_drawn_asset(db, source_id, definition, fid)
        if row is None or not row.s3_object_name:
            continue
        data = await file_storage.download(row.s3_object_name)
        key = await file_storage.upload(data=data, filename=row.filename, content_type=row.content_type, prefix=asset_prefix(target_id))
        twin = FileAttachment(
            user_id=user_id,
            filename=row.filename,
            content_type=row.content_type,
            size=row.size,
            s3_object_name=key,
            sha256=row.sha256,
            source=DASHBOARD_ASSET_SOURCE,
        )
        db.add(twin)
        await db.flush()
        copied[fid] = str(twin.id)
    return copied


async def delete_dashboard_assets(db: AsyncSession, dashboard_id: uuid.UUID) -> list[str]:
    """Delete the rows of a dashboard's assets; return their object keys, purged by the caller after its commit."""
    from sqlalchemy import delete

    prefix = asset_prefix(dashboard_id) + "/"
    result = await db.execute(
        delete(FileAttachment)
        .where(
            FileAttachment.source == DASHBOARD_ASSET_SOURCE,
            FileAttachment.s3_object_name.startswith(prefix, autoescape=True),
        )
        .returning(FileAttachment.s3_object_name)
        .execution_options(synchronize_session=False)
    )
    return [key for key in result.scalars().all() if key]


__all__ = [
    "DASHBOARD_ASSET_SOURCE",
    "MAX_ASSETS",
    "MAX_ASSET_BYTES",
    "DashboardAssetError",
    "asset_prefix",
    "delete_dashboard_assets",
    "load_drawn_asset",
    "sniff_image_type",
    "store_asset",
]
