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

"""The files the Video Studio stores and reads.

Every output is a ``FileAttachment`` of its creator - the take's mezzanine
and poster, a render's MP4, poster and captions, a clip, an MFA screenshot -
so it is listed in their Files and downloadable there. The studio serves its
media through its own routes, which authorise by the take, clip or project
(``api/v1/video_studio.py``), never by the file row alone: sharing a project
shares what it shows, like a deck shares its pictures.

Large media move between object storage and the worker's disk without being
held in memory (``FileStorageService.upload_path`` / ``download_to_path``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.utils import format_exception_message, safe_uuid
from app.models.file_attachment import FileAttachment
from app.services.file_storage import file_storage, guess_content_type

logger = logging.getLogger(__name__)

SOURCE_TAKE = "video_take"
SOURCE_CLIP = "video_clip"
SOURCE_RENDER = "video_render"
SOURCE_MFA = "video_mfa"
SOURCE_VOICE = "video_voice"
#: An AI-composed music track.
SOURCE_MUSIC = "video_music"
#: A picture, a sound or a video file the person uploaded into the editor.
SOURCE_UPLOAD = "video_upload"
#: The frame an AI shot starts from, cut from a recording, a shot or a video file:
#: a file of the conversation, kept when the shot is deleted (the chat may show it).
SOURCE_CLIP_START_FRAME = "clip-start-frame"
STUDIO_SOURCES = frozenset(
    {
        SOURCE_TAKE,
        SOURCE_CLIP,
        SOURCE_RENDER,
        SOURCE_MFA,
        SOURCE_VOICE,
        SOURCE_MUSIC,
        SOURCE_UPLOAD,
        SOURCE_CLIP_START_FRAME,
    }
)


def _prefix(user_id: object) -> str:
    uid = safe_uuid(str(user_id)) if user_id else None
    return f"video-studio/{uid or 'shared'}"


async def store_bytes(
    data: bytes,
    *,
    filename: str,
    user_id: object,
    source: str,
    content_type: str | None = None,
    conversation_id: object = None,
    description: str | None = None,
) -> FileAttachment:
    """Upload *data* and record it as a file of *user_id*, on a session of its own."""
    content_type = content_type or guess_content_type(filename)
    object_name = await file_storage.upload(data, filename, content_type, prefix=_prefix(user_id))
    return await _record(
        object_name,
        filename=filename,
        size=len(data),
        sha256=await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest()),
        user_id=user_id,
        source=source,
        content_type=content_type,
        conversation_id=conversation_id,
        description=description,
    )


async def store_path(
    path: str,
    *,
    filename: str,
    user_id: object,
    source: str,
    content_type: str | None = None,
    conversation_id: object = None,
    description: str | None = None,
) -> FileAttachment:
    """Upload a local file (streamed from disk) and record it, on a session of its own."""
    content_type = content_type or guess_content_type(filename)
    ext = Path(filename).suffix.lower()
    object_name = f"{_prefix(user_id)}/{uuid.uuid4().hex}{ext}"
    await file_storage.upload_path(path, object_name, content_type)
    return await _record(
        object_name,
        filename=filename,
        size=os.path.getsize(path),
        sha256=await asyncio.to_thread(_file_sha256, path),
        user_id=user_id,
        source=source,
        content_type=content_type,
        conversation_id=conversation_id,
        description=description,
    )


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _record(
    object_name: str,
    *,
    filename: str,
    size: int,
    sha256: str,
    user_id: object,
    source: str,
    content_type: str,
    conversation_id: object,
    description: str | None,
) -> FileAttachment:
    row = FileAttachment(
        user_id=safe_uuid(str(user_id)) if user_id else None,
        conversation_id=safe_uuid(str(conversation_id)) if conversation_id else None,
        filename=filename[:500],
        content_type=content_type,
        size=size,
        s3_object_name=object_name,
        sha256=sha256,
        source=source,
        description=description,
    )
    async with async_session() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def load_file(file_id: object) -> FileAttachment | None:
    fid = safe_uuid(str(file_id)) if file_id else None
    if fid is None:
        return None
    async with async_session() as db:
        return (await db.execute(select(FileAttachment).where(FileAttachment.id == fid))).scalar_one_or_none()


async def download_file(file_id: object, path: str) -> FileAttachment | None:
    """Write a stored file to *path*; ``None`` when the file is gone."""
    from app.services.file_storage import FileNotFoundInStorageError

    row = await load_file(file_id)
    if row is None:
        return None
    try:
        await file_storage.download_to_path(row.s3_object_name, path)
    except FileNotFoundInStorageError:
        return None
    return row


async def forget_files(db: AsyncSession, file_ids: Iterable[object]) -> list[str]:
    """Delete the rows of *file_ids* in *db*'s transaction (not committed): the storage keys to purge once it commits.

    A file a video needs is kept (``retention.files_in_use``): the frame
    a shot it plays starts from, a picture a screen insert shows, what a ready
    render drew. Deleting the medium it came with never takes it from the video.
    """
    from app.services.video_studio import retention

    ids = [fid for fid in (safe_uuid(str(f)) for f in file_ids if f) if fid is not None]
    if not ids:
        return []
    needed = await retention.files_in_use(db, ids)
    if needed:
        logger.info("Video Studio files kept: %d of %d a video needs", len(needed), len(ids))
        ids = [fid for fid in ids if str(fid) not in needed]
        if not ids:
            return []
    names = (await db.execute(select(FileAttachment.s3_object_name).where(FileAttachment.id.in_(ids)))).scalars().all()
    await db.execute(delete(FileAttachment).where(FileAttachment.id.in_(ids)))
    return [name for name in names if name]


async def delete_files(file_ids: Iterable[object]) -> None:
    """Delete the rows and objects of *file_ids*, best effort (a file already gone is skipped)."""
    wanted = [fid for fid in file_ids if fid]
    if not wanted:
        return
    async with async_session() as db:
        names = await forget_files(db, wanted)
        await db.commit()
    for name in names:
        try:
            await file_storage.delete(name)
        except Exception as exc:  # best effort: the row is gone, the sweep keeps nothing
            logger.debug("Video Studio file %s not deleted: %s", name, format_exception_message(exc))
