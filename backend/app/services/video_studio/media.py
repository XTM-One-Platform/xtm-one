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

"""Playing the Video Studio's media in a browser: confined tokens and byte ranges.

A ``<video>`` element cannot send the ``Authorization`` header, and seeking in
a video is a byte-range request the player makes by itself. So the page asks
for a media token (``POST /video-studio/media-token``), which the API mints only
after checking the caller may read the take, clip, render or MFA screenshot the
file belongs to; the player then fetches ``/video-studio/media/{token}``.

The token is built like the deck render token (``presentation_export``): a JWT
with a ``scope`` claim, which ``get_current_user`` refuses as a session, bound
to ONE file and to its reader, for :data:`MEDIA_TOKEN_TTL`. A download token
also carries the name the file is saved under - the video's title and
quality, signed, so the browser saves "Launch film - 1080p60.mp4" rather than
the storage name. The byte route answers ``Range`` requests with ``206`` and
at most :data:`MAX_RANGE_BYTES` per answer (an open-ended ``bytes=0-``
included: the player asks for the rest), through :func:`serve_file`, which the
public video link's route serves with too.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings
from app.core.security import JWT_CLOCK_SKEW_LEEWAY

MEDIA_TOKEN_SCOPE = "video_studio_media"  # noqa: S105 - a claim value, not a secret
MEDIA_TOKEN_TTL = timedelta(minutes=45)
MAX_RANGE_BYTES = 4 * 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class MediaTokenError(ValueError):
    """Not a live media token."""


@dataclass(frozen=True, slots=True)
class MediaClaims:
    file_id: uuid.UUID
    user_id: uuid.UUID
    expires: int
    #: A download token's file name: the answer is an attachment saved under it.
    filename: str | None = None


def mint_media_token(
    file_id: uuid.UUID, user_id: uuid.UUID, *, filename: str | None = None, windowed: bool = False
) -> tuple[str, int]:
    """A token reading one file. *windowed* (the editor's preview): issued at the start of the
    current :data:`MUSIC_TOKEN_WINDOW`, so every preview planned within it gives the file the same URL."""
    now = _window_start() if windowed else datetime.now(UTC)
    expires = now + MEDIA_TOKEN_TTL
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "scope": MEDIA_TOKEN_SCOPE,
        "file_id": str(file_id),
        "iat": int(now.timestamp()),
        "exp": expires,
    }
    if filename:
        payload["dl"] = filename
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), int(expires.timestamp())


def decode_media_token(token: str) -> MediaClaims:
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise MediaTokenError("invalid or expired media token") from exc
    if payload.get("scope") != MEDIA_TOKEN_SCOPE:
        raise MediaTokenError("not a media token")
    filename = payload.get("dl")
    try:
        return MediaClaims(
            uuid.UUID(str(payload["file_id"])),
            uuid.UUID(str(payload["sub"])),
            int(payload["exp"]),
            filename=str(filename) if filename else None,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaTokenError("malformed media token") from exc


#: A music bed's token: what decides the bed rather than one file - a
#: built-in score is made for the video, never stored as a file of anyone.
MUSIC_TOKEN_SCOPE = "video_studio_music"  # noqa: S105 - a claim value, not a secret
#: The same bed for the same reader gets the same token within this window, so
#: the editor's player keeps its source across previews (a new URL reloads it).
MUSIC_TOKEN_WINDOW = timedelta(minutes=15)


def _window_start() -> datetime:
    """When the current token window began: a token issued then is the same for the same claims."""
    window = int(MUSIC_TOKEN_WINDOW.total_seconds())
    return datetime.fromtimestamp(int(datetime.now(UTC).timestamp()) // window * window, UTC)


def mint_music_token(bed: dict, user_id: uuid.UUID) -> tuple[str, int]:
    issued = _window_start()
    expires = issued + MEDIA_TOKEN_TTL
    payload = {
        "sub": str(user_id),
        "scope": MUSIC_TOKEN_SCOPE,
        "bed": bed,
        "iat": int(issued.timestamp()),
        "exp": expires,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), int(expires.timestamp())


def decode_music_token(token: str) -> tuple[dict, uuid.UUID]:
    """``(bed claims, reader)`` of a live music token, or :class:`MediaTokenError`."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise MediaTokenError("invalid or expired music token") from exc
    if payload.get("scope") != MUSIC_TOKEN_SCOPE or not isinstance(payload.get("bed"), dict):
        raise MediaTokenError("not a music token")
    try:
        return payload["bed"], uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaTokenError("malformed music token") from exc


#: A spoken narration's token: the digest of the narration in the voice
#: cache, which only the preview that planned it knows. Windowed like a bed's,
#: so the editor's players keep their source across previews.
VOICE_TOKEN_SCOPE = "video_studio_voice"  # noqa: S105 - a claim value, not a secret
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def mint_voice_token(digest: str, user_id: uuid.UUID) -> tuple[str, int]:
    issued = _window_start()
    expires = issued + MEDIA_TOKEN_TTL
    payload = {
        "sub": str(user_id),
        "scope": VOICE_TOKEN_SCOPE,
        "voice": digest,
        "iat": int(issued.timestamp()),
        "exp": expires,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), int(expires.timestamp())


def decode_voice_token(token: str) -> tuple[str, uuid.UUID]:
    """``(narration digest, reader)`` of a live voice token, or :class:`MediaTokenError`."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise MediaTokenError("invalid or expired voice token") from exc
    digest = payload.get("voice")
    if payload.get("scope") != VOICE_TOKEN_SCOPE or not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise MediaTokenError("not a voice token")
    try:
        return digest, uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaTokenError("malformed voice token") from exc


FIGURE_TOKEN_SCOPE = "video_studio_figure"  # noqa: S105 - a claim value, not a secret


def mint_figure_token(digest: str, user_id: uuid.UUID) -> tuple[str, int]:
    """A token naming one figure still by its digest (``figures.preview_object``), for the preview that resolved it."""
    issued = _window_start()
    expires = issued + MEDIA_TOKEN_TTL
    payload = {
        "sub": str(user_id),
        "scope": FIGURE_TOKEN_SCOPE,
        "figure": digest,
        "iat": int(issued.timestamp()),
        "exp": expires,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), int(expires.timestamp())


def decode_figure_token(token: str) -> tuple[str, uuid.UUID]:
    """``(still digest, reader)`` of a live figure token, or :class:`MediaTokenError`."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise MediaTokenError("invalid or expired figure token") from exc
    digest = payload.get("figure")
    if payload.get("scope") != FIGURE_TOKEN_SCOPE or not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise MediaTokenError("not a figure token")
    try:
        return digest, uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaTokenError("malformed figure token") from exc


INSERT_TOKEN_SCOPE = "video_studio_insert"  # noqa: S105 - a claim value, not a secret


def mint_insert_token(digest: str, user_id: uuid.UUID) -> tuple[str, int]:
    """A token naming one screen insert's preview build by its digest (``insert_builds.build_object``)."""
    issued = _window_start()
    expires = issued + MEDIA_TOKEN_TTL
    payload = {
        "sub": str(user_id),
        "scope": INSERT_TOKEN_SCOPE,
        "insert": digest,
        "iat": int(issued.timestamp()),
        "exp": expires,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm), int(expires.timestamp())


def decode_insert_token(token: str) -> tuple[str, uuid.UUID]:
    """``(build digest, reader)`` of a live insert token, or :class:`MediaTokenError`."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise MediaTokenError("invalid or expired insert token") from exc
    digest = payload.get("insert")
    if payload.get("scope") != INSERT_TOKEN_SCOPE or not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise MediaTokenError("not an insert token")
    try:
        return digest, uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaTokenError("malformed insert token") from exc


_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_EXTENSIONS = {"video": "mp4", "poster": "jpg", "captions": "vtt", "start_frame": "jpg"}


def download_filename(title: str, asset: str, *, height: int | None = None, fps: int | None = None) -> str:
    """The name a render's file is saved under: ``"<title> - 1080p60.mp4"``, ``"<title>.vtt"``.

    Characters no file system takes are dropped; an empty title reads "video".
    """
    base = _FILENAME_UNSAFE.sub(" ", title or "").strip().strip(".") or "video"
    base = re.sub(r"\s+", " ", base)[:150]
    if asset == "video" and height:
        base = f"{base} - {height}p{fps or ''}"
    return f"{base}.{_EXTENSIONS.get(asset, 'bin')}"


async def serve_file(row: Any, range_header: str | None, headers: dict[str, str]):
    """*row*'s bytes as a media route answers them: the asked range (206), or the whole file, streamed.

    *headers* are the route's own (disposition, caching); the range headers
    are added here. Raises ``FileNotFoundInStorageError`` when the object is
    gone, for the route to answer its own 404.
    """
    from fastapi.responses import Response, StreamingResponse

    from app.services.file_storage import file_storage

    size = int(row.size or 0)
    headers = {**headers, "Accept-Ranges": "bytes"}
    try:
        byte_range = parse_range(range_header, size)
    except ValueError:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    media_type = headers.get("Content-Type", row.content_type)
    object_name = row.s3_object_name
    if byte_range is not None:
        start, end = byte_range
        data = await file_storage.read_range(object_name, start, end - start + 1)
        headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{size}"
        headers["Content-Length"] = str(len(data))
        return Response(content=data, status_code=206, headers=headers, media_type=media_type)

    async def body():
        offset = 0
        while offset < size:
            chunk = await file_storage.read_range(object_name, offset, min(MAX_RANGE_BYTES, size - offset))
            if not chunk:
                return
            offset += len(chunk)
            yield chunk

    headers["Content-Length"] = str(size)
    return StreamingResponse(body(), headers=headers, media_type=media_type)


def answer_bytes(header: str | None, size: int) -> int:
    """How many bytes :func:`serve_file` answers *header* with on a file of *size*: what a byte budget charges."""
    try:
        byte_range = parse_range(header, size)
    except ValueError:
        return 0
    return size if byte_range is None else byte_range[1] - byte_range[0] + 1


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``(start, end inclusive)`` of a single ``bytes=`` range, clamped; ``None`` = the whole file.

    Raises ``ValueError`` for a range that cannot be satisfied (the ``416``).
    """
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match or size <= 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        length = min(int(last), size)
        start, end = size - length, size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("unsatisfiable range")
    end = min(end, start + MAX_RANGE_BYTES - 1)
    return start, end
