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

"""What a render keeps for the next renders of the same video, keyed by what it was made from.

A second render of an unchanged revision at another quality redoes nothing
that does not depend on the quality. Some of it was kept already: the
narrations and their transcript checks by the digest of what is said
(``voice``, ``voice_check``), the figures by what they draw - a sharper one
serving a lower quality (``figure_cache``). This module keeps the rest, each
under ``<kind>/<digest>`` of everything it is made from:

* ``beds`` - the built-in score composed and prepared for the video's length
  and scenes (``music.make_bed``), a file;
* ``tracks`` - the screen inserts followed through their footage
  (``render._track_inserts``), their quads frame by frame, a JSON document.

Kept in the object store, so any worker finds them, and on this worker's disk
for the next render here. Best effort both ways: what is not kept is made
again. An object unread for :data:`KEPT_DAYS` days goes with the hourly sweep
(:func:`sweep`), a file on the disk after :data:`KEPT_ON_DISK_SECONDS`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

PREFIX = "video-studio/render-cache"
#: What this worker keeps on its disk, beside the renders' working directories.
ROOT = Path(tempfile.gettempdir()) / "xtm-render-cache"
#: An object stored this long ago goes with the hourly sweep; a render that needs it makes it again.
KEPT_DAYS = 14
#: A file on this worker's disk nothing used for this long goes with the working directories' sweep.
KEPT_ON_DISK_SECONDS = 24 * 3600


def object_name(kind: str, digest: str, suffix: str = "") -> str:
    return f"{PREFIX}/{kind}/{digest}{suffix}"


def _local(kind: str, digest: str, suffix: str) -> Path:
    return ROOT / kind / f"{digest}{suffix}"


def _link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        shutil.copyfile(source, part)
        part.replace(target)
    finally:
        with contextlib.suppress(OSError):
            part.unlink()


async def get_file(kind: str, digest: str, path: str, *, suffix: str = "") -> bool:
    """Whether a file kept under *kind* / *digest* was written to *path*: from this worker's disk, else the store."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    local = _local(kind, digest, suffix)
    try:
        if local.is_file() and local.stat().st_size > 0:
            local.touch()
            _link(local, Path(path))
            return True
        part = Path(f"{path}.{uuid.uuid4().hex[:8]}.part")
        try:
            await file_storage.download_to_path(object_name(kind, digest, suffix), str(part), missing_ok=True)
            if part.stat().st_size <= 0:
                return False
            part.replace(path)
        finally:
            with contextlib.suppress(OSError):
                part.unlink()
        _link(Path(path), local)
        return True
    except FileNotFoundInStorageError:
        return False
    except Exception as exc:  # a cache that cannot be read is a miss
        logger.info("Render cache %s/%s not read: %s", kind, digest[:12], format_exception_message(exc))
        return False


async def put_file(kind: str, digest: str, path: str, *, suffix: str = "") -> None:
    """Keep the file at *path* under *kind* / *digest*, on this worker's disk and in the store."""
    from app.services.file_storage import file_storage

    try:
        _link(Path(path), _local(kind, digest, suffix))
        await file_storage.upload_path(path, object_name(kind, digest, suffix))
    except Exception as exc:  # unkept: made again by the next render that needs it
        logger.info("Render cache %s/%s not kept: %s", kind, digest[:12], format_exception_message(exc))


async def get_json(kind: str, digest: str) -> object | None:
    """The JSON value kept under *kind* / *digest*, or ``None``."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    local = _local(kind, digest, ".json")
    try:
        if local.is_file():
            local.touch()
            return json.loads(local.read_text(encoding="utf-8"))
        data = await file_storage.download(object_name(kind, digest, ".json"))
        value = json.loads(data.decode("utf-8"))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return value
    except FileNotFoundInStorageError:
        return None
    except Exception as exc:
        logger.info("Render cache %s/%s not read: %s", kind, digest[:12], format_exception_message(exc))
        return None


async def put_json(kind: str, digest: str, value: object) -> None:
    """Keep *value* under *kind* / *digest*, on this worker's disk and in the store."""
    from app.services.file_storage import file_storage

    body = json.dumps(value, default=str).encode("utf-8")
    try:
        local = _local(kind, digest, ".json")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        await file_storage.upload_to(body, object_name(kind, digest, ".json"), "application/json")
    except Exception as exc:
        logger.info("Render cache %s/%s not kept: %s", kind, digest[:12], format_exception_message(exc))


async def sweep(*, now: datetime | None = None) -> int:
    """Delete what the store kept more than :data:`KEPT_DAYS` days ago; how many objects."""
    from app.services.file_storage import file_storage

    cutoff = (now or datetime.now(UTC)) - timedelta(days=KEPT_DAYS)
    deleted = 0
    for name, modified in await file_storage.list_objects_modified(f"{PREFIX}/"):
        if modified is not None and modified < cutoff:
            await file_storage.delete(name)
            deleted += 1
    return deleted


def sweep_disk(*, now: float | None = None) -> int:
    """Remove the files of this worker's cache nothing used for :data:`KEPT_ON_DISK_SECONDS`; how many."""
    now = time.time() if now is None else now
    removed = 0
    if not ROOT.is_dir():
        return 0
    for path in ROOT.rglob("*"):
        try:
            if path.is_file() and now - path.stat().st_mtime > KEPT_ON_DISK_SECONDS:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
