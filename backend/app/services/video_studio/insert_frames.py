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

"""A screen insert's frames drawn once on a worker, served to every page, attempt and render after it.

A frame of a screen insert costs a page many times a plain frame (``screen_insert.compose``: its bezel, its warp,
its matte, its light, its audits): a 10 s scene with one insert took 12 minutes to preview at 1080p, and every page
that drew it again - a page starting elsewhere, an attempt run again, the next preview of the same revision - paid
it again. Yet a frame is a pure function of what it is drawn from: the insert's source and footage files, every
field it is drawn by, and the frame - the source frame shown, the footage frame under it, the host's size, a push's
scale. So it is drawn once per worker and kept:

* **On the worker's disk.** One folder per insert drawing (``compositor.insert_drawn_by``, :func:`folder_key`),
  shared by every render process of the worker: the pages of a render, an attempt that runs again, the next preview
  of the scene, a whole render drawn at the same size. Each frame is kept as the page reads it - its PNG and its
  glow's - with its audit and the pictures a failing audit shows (:class:`Frames`). When an insert shows in the
  timeline is not part of its key: a scene preview and a whole render share their frames.
* **Held to its room.** A folder untouched for :data:`KEPT_ON_DISK_SECONDS` is swept (:func:`sweep_disk`), and
  every :data:`CHECK_EVERY_BYTES` written the folders are held within ``VIDEO_STUDIO_INSERT_FRAMES_MB``
  (:func:`kept_bytes`; 0 keeps none) - never more than a quarter of what their disk has for them -, the least
  recently used going first (:func:`bound`). The worker's temporary directory holds them beside the renders'
  working directories: a deployment sizes it for both.
* **Keyed by the code that draws it.** :func:`code_digest` digests the modules a frame is drawn by: a release that
  draws an insert otherwise never serves a frame drawn before it.

Best effort both ways: a frame not kept is drawn, a frame that cannot be read is drawn again.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Where a worker keeps its insert frames, one folder per insert drawing, beside the renders' working directories.
ROOT = Path(tempfile.gettempdir()) / "xtm-insert-frames"
#: A folder nothing read or wrote for this long goes with the working directories' sweep: longer than a render runs.
KEPT_ON_DISK_SECONDS = 6 * 3600
#: What a process writes between two checks of what the folders hold.
CHECK_EVERY_BYTES = 256 * 1024**2
#: The modules a frame is drawn by (``app.services.video_studio.<name>``): their code is part of every key.
DRAWN_BY = ("screen_insert", "screens", "surface_refine", "pictures", "tracking", "compositor")
#: Moved when what a folder keeps changes shape.
VERSION = 1
#: The pictures a failing frame shows (``screen_insert.Layer``): what each is kept as, ``compositor._audited``'s.
EVIDENCE = {
    "proof": "jpg",
    "residual_crop": "png",
    "halo_crop": "png",
    "foreground_crop": "png",
    "rim_crop": "png",
    "through_crop": "png",
    "spill_frame": "jpg",
}

_written = 0
_written_lock = threading.Lock()
#: The folders this process found past the room on their own (:func:`bound`): they keep what they hold, no more.
_full: set[Path] = set()


def kept_bytes() -> int:
    """The most the folders hold (``VIDEO_STUDIO_INSERT_FRAMES_MB``), less when their disk has less than four times it
    for them (:func:`bound`); 0: no frame is kept."""
    from app.core.config import settings

    return max(0, int(settings.video_studio_insert_frames_mb or 0)) << 20


@functools.cache
def code_digest() -> str:
    """The digest of the modules a frame is drawn by (:data:`DRAWN_BY`), read once per process."""
    digest = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for name in DRAWN_BY:
        try:
            digest.update((here / f"{name}.py").read_bytes())
        except OSError:
            digest.update(name.encode("utf-8"))
    return digest.hexdigest()[:16]


def folder_key(drawn_by: dict) -> str:
    """The folder of an insert drawing: what it is drawn by (``compositor.insert_drawn_by``) and the code drawing it."""
    payload = {"version": VERSION, "code": code_digest(), "drawn_by": drawn_by}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def frame_name(index: int, footage: int | None, size: tuple[int, int], scale: float, visibility: float = 1.0) -> str:
    """A frame of a drawing: the source frame shown, the footage frame under it, the host's size, a push's scale and,
    for a keyed insert held on its surface partly shown, how much of it shows, in permille."""
    shown = f"-v{round(visibility * 1000):04d}" if visibility < 1.0 else ""
    return f"{index}-{'s' if footage is None else footage}-{size[0]}x{size[1]}-{scale:.4f}{shown}"


@dataclass
class Kept:
    """A frame read back: what the page reads, its audit as ``FrameAudit.as_dict`` wrote it (``None``: not matted),
    and the pictures its failing audit shows, by kind (:data:`EVIDENCE`)."""

    drawn: bytes
    glow: bytes | None
    audit: dict | None
    evidence: dict[str, Path]


def _write(path: Path, data: bytes) -> None:
    part = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        part.write_bytes(data)
        part.replace(path)
    finally:
        with contextlib.suppress(OSError):
            part.unlink()


class Frames:
    """The frames of one insert drawing kept on this worker's disk (:func:`folder_key`)."""

    def __init__(self, key: str) -> None:
        self.folder = ROOT / key

    def get(self, name: str, *, evidence: bool) -> Kept | None:
        """Frame *name* as it was kept; ``None`` when it was not, or was drawn without the pictures its failing audit
        shows and *evidence* wants them."""
        if not kept_bytes():
            return None
        try:
            meta = json.loads((self.folder / f"{name}.json").read_text(encoding="utf-8"))
            if evidence and meta.get("failing") and not meta.get("evidenced"):
                return None
            drawn = (self.folder / f"{name}.png").read_bytes()
            glow = (self.folder / f"{name}.glow.png").read_bytes() if meta.get("glow") else None
            shown = {kind: self.folder / f"{name}.{kind}.{suffix}" for kind, suffix in EVIDENCE.items() if kind in (meta.get("evidence") or [])}
            if any(not path.is_file() for path in shown.values()):
                return None
            with contextlib.suppress(OSError):
                os.utime(self.folder)
            return Kept(drawn, glow, meta.get("audit"), shown)
        except (OSError, ValueError):
            return None

    def put(self, name: str, drawn: bytes, glow: bytes | None, audit: Any, pictures: dict[str, Any], *, evidenced: bool) -> None:
        """Keep frame *name*: its PNG, its glow's, its *audit* (a ``FrameAudit``, ``None``: not matted) and *pictures*
        (``kind -> PIL image``) - whether they were drawn being *evidenced*. Its record is written last: a frame
        is read only once whole."""
        global _written
        if not kept_bytes() or self.folder in _full:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            size = len(drawn) + len(glow or b"")
            _write(self.folder / f"{name}.png", drawn)
            if glow is not None:
                _write(self.folder / f"{name}.glow.png", glow)
            kept = []
            for kind, picture in pictures.items():
                if picture is None or kind not in EVIDENCE:
                    continue
                path = self.folder / f"{name}.{kind}.{EVIDENCE[kind]}"
                part = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
                try:
                    if EVIDENCE[kind] == "jpg":
                        picture.save(part, format="JPEG", quality=88)
                    else:
                        picture.save(part, format="PNG")
                    part.replace(path)
                finally:
                    with contextlib.suppress(OSError):
                        part.unlink()
                kept.append(kind)
                size += path.stat().st_size
            meta = {
                "glow": glow is not None,
                "audit": audit.as_dict() if audit is not None else None,
                "failing": bool(audit is not None and audit.failing),
                "evidenced": evidenced,
                "evidence": kept,
            }
            _write(self.folder / f"{name}.json", json.dumps(meta).encode("utf-8"))
        except Exception as exc:  # a frame not kept is drawn again by the next page that needs it
            logger.info("Insert frame %s/%s not kept: %s", self.folder.name[:12], name, format_exception_message(exc))
            return
        with _written_lock:
            _written += size
            due = _written >= CHECK_EVERY_BYTES
            if due:
                _written = 0
        if due:
            bound(keep=self.folder)


def _folder_bytes(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError), os.scandir(path) as entries:
        for entry in entries:
            with contextlib.suppress(OSError):
                total += entry.stat().st_size
    return total


def bound(*, keep: Path | None = None) -> int:
    """Hold the folders within :func:`kept_bytes` and a quarter of what their disk has for them, the least recently
    used going first, never *keep* (the folder being written); how many went. *keep* past the room on its own - a
    long insert at 2160p - keeps the frames it holds and takes no more in this process."""
    if not ROOT.is_dir():
        return 0
    folders = []
    for path in ROOT.iterdir():
        with contextlib.suppress(OSError):
            if path.is_dir():
                folders.append((path.stat().st_mtime, path, _folder_bytes(path)))
    held = sum(size for _mtime, _path, size in folders)
    try:
        free = shutil.disk_usage(ROOT).free
    except OSError:
        free = 0
    room = min(kept_bytes(), (held + free) // 4)
    removed = 0
    for _mtime, path, size in sorted(folders, key=lambda item: item[0]):
        if held <= room:
            break
        if keep is not None and path == keep:
            continue
        shutil.rmtree(path, ignore_errors=True)
        held -= size
        removed += 1
    if keep is not None and held > room:
        _full.add(keep)
        logger.info("Insert frames: drawing %s holds %d MiB, past its %d MiB: it takes no more frames", keep.name[:12], held >> 20, room >> 20)
    if removed:
        logger.info("Insert frames: %d drawings of this worker removed, %d MiB kept within %d MiB", removed, held >> 20, room >> 20)
    return removed


def sweep_disk(*, now: float | None = None) -> int:
    """Remove the insert drawings of this worker nothing read or wrote for :data:`KEPT_ON_DISK_SECONDS`; how many."""
    now = time.time() if now is None else now
    removed = 0
    if not ROOT.is_dir():
        return 0
    for path in ROOT.iterdir():
        try:
            if path.is_dir() and now - path.stat().st_mtime > KEPT_ON_DISK_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
