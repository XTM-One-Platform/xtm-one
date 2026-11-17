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

"""A take's pacing pass, checkpointed so the next worker resumes it (#10030).

A recording whose worker is stopped by a rollout is finished by the next worker from the keyframes its capture
stored (``runners._resumed_take``, #9812). Its pacing pass (``scroll_pacing.pace``) was then run again from its first
scroll: under deploys a few minutes apart a long 60 fps take never got past it. Now every scroll it paces, and its
transitions once paced, are a *step* handed to ``on_step``: what the take keeps of it, the frames it drew and the
keyframes they replace. The worker packs the steps done since its last checkpoint, with the frames they drew, into a
chunk (a zip stored as a take's file) every :data:`CHUNK_SECONDS`, and names the chunks on the take
(``capture.pacing``, bound to the keyframes file they were paced from). The next worker unpacks them before its pass
and ``pace`` reuses each step whose scroll is the same, drawing only the rest: the frames are the same files, byte for
byte, and the take's notes say where it resumed (:func:`resumed_note`).
"""

from __future__ import annotations

import json
import threading
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

#: The chunk format; a chunk of another version is not read (the pass is run again from its first step).
VERSION = 1
MANIFEST = "pacing.json"
#: Where a running take names its chunks (``capture.pacing``): ``{keyframes, fps, pixel_ratio, frames, chunks}``.
CAPTURE_KEY = "pacing"
#: How often a worker stores the steps it paced since its last chunk, in seconds of pacing.
CHUNK_SECONDS = 20.0
#: The step that holds a take's transitions, once paced (``transition_pacing.pace_transitions``).
TRANSITIONS = "transitions"


def scroll_key(serial: int) -> str:
    """The step of the scroll paced *serial*-th (``scroll_pacing.pace``)."""
    return f"s{serial}"


def span_of(span: Sequence[float]) -> list[float]:
    """A scroll's span as a step compares it: rounded to the 0.1 ms its log keeps."""
    return [round(float(span[0]), 4), round(float(span[1]), 4)]


def scroll_step(
    span: Sequence[float], kept: dict, frames: Sequence[tuple[float, str]], used: Iterable[int], seen: dict
) -> dict:
    """One scroll paced, as a chunk keeps it: its span, what the take keeps of it, the frames it drew, the keyframes
    they replace and what a canvas zoom read of the boxes over its canvas (``seen``)."""
    return {
        "span": span_of(span),
        "kept": kept,
        "frames": [[float(t), str(name)] for t, name in frames],
        "used": sorted(int(i) for i in used),
        "seen": {k: sorted(v) if isinstance(v, set | frozenset) else v for k, v in (seen or {}).items()},
    }


def read_scroll_step(record: dict) -> tuple[dict, list[tuple[float, str]], set[int], dict]:
    """``(kept, frames, used, seen)`` of a step :func:`scroll_step` wrote."""
    seen = dict(record.get("seen") or {})
    for name in ("standing", "riding"):
        if name in seen:
            seen[name] = set(seen[name])
    if "over" in seen:
        seen["over"] = [tuple(box) for box in seen["over"]]
    frames = [(float(t), str(name)) for t, name in record.get("frames") or []]
    return dict(record["kept"]), frames, {int(i) for i in record.get("used") or []}, seen


def transitions_step(frames: Sequence[tuple[float, str]], used: Iterable[int], motion: Sequence[Any]) -> dict:
    """A take's transitions paced: the frames drawn, the keyframes they replace and each event's ``motion`` as kept."""
    return {
        "frames": [[float(t), str(name)] for t, name in frames],
        "used": sorted(int(i) for i in used),
        "motion": list(motion),
    }


def drawn_names(record: dict) -> list[str]:
    """The files a step names as its frames."""
    return [str(name) for _t, name in record.get("frames") or []]


def usable(record: dict, workdir: str | Path) -> bool:
    """Whether every frame a step names is in *workdir* (a keyframe, or a frame a chunk restored)."""
    root = Path(workdir)
    return all((root / name).is_file() for name in drawn_names(record))


def point(*, keyframes: object, fps: int, pixel_ratio: float, frames: int, chunks: Sequence[object] = ()) -> dict:
    """What a running take names of its pacing checkpoint (``capture.pacing``)."""
    return {
        "keyframes": str(keyframes),
        "fps": int(fps),
        "pixel_ratio": float(pixel_ratio),
        "frames": int(frames),
        "chunks": [str(c) for c in chunks],
    }


def pacing_point(capture: dict | None, *, keyframes: object) -> dict | None:
    """The checkpoint a take's *capture* names when it was paced from *keyframes*; ``None`` without one."""
    found = capture.get(CAPTURE_KEY) if isinstance(capture, dict) else None
    if not isinstance(found, dict) or str(found.get("keyframes")) != str(keyframes) or not found.get("chunks"):
        return None
    return found


def matches(found: dict | None, *, fps: int, pixel_ratio: float, frames: int) -> bool:
    """Whether a checkpoint was paced at this rate, this ratio and on as many keyframes."""
    return (
        isinstance(found, dict)
        and int(found.get("fps") or 0) == int(fps)
        and float(found.get("pixel_ratio") or 0) == float(pixel_ratio)
        and int(found.get("frames") or -1) == int(frames)
    )


def pack_chunk(out_path: str | Path, steps: dict[str, dict], *, workdir: str | Path, keyframes: Iterable[str]) -> int:
    """Write *steps* (key -> record) and the frames they drew (every file they name in *workdir* but the *keyframes*,
    which the take's keyframes file holds already) to the zip *out_path*; the files packed."""
    root = Path(workdir)
    skip = set(keyframes)
    names = list(dict.fromkeys(name for record in steps.values() for name in drawn_names(record) if name not in skip))
    packed = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MANIFEST, json.dumps({"version": VERSION, "steps": steps}, separators=(",", ":"), default=str))
        for name in names:
            path = root / name
            if path.is_file():
                archive.write(path, arcname=f"frames/{name}")
                packed += 1
    return packed


def unpack_chunk(zip_path: str | Path, workdir: str | Path) -> dict[str, dict]:
    """The steps a chunk holds, its frames written to *workdir*; ``{}`` for a chunk unreadable or of another version."""
    root = Path(workdir)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            if manifest.get("version") != VERSION or not isinstance(manifest.get("steps"), dict):
                return {}
            for info in archive.infolist():
                if not info.filename.startswith("frames/") or info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name or name != info.filename[len("frames/") :]:
                    return {}
                (root / name).write_bytes(archive.read(info))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError):
        return {}
    return {str(k): v for k, v in manifest["steps"].items() if isinstance(v, dict)}


def resumed_at(done: dict[str, dict] | None, reused: Iterable[str]) -> int | None:
    """The step (from 1) a pass resumed at: the first scroll it paced itself; ``None`` when it reused nothing."""
    serials = sorted(int(key[1:]) for key in reused if key.startswith("s") and key[1:].isdigit())
    if not done or not serials:
        return None
    step = 0
    while step in serials:
        step += 1
    return step + 1


def resumed_note(step: int, reused: int) -> str:
    """What a take's notes say of a pass resumed from its checkpoint."""
    return f"Pacing resumed at step {step} from the checkpoint its stopped worker stored ({reused} step(s) not paced again)."


class Pending:
    """The steps a pass hands over from its thread, taken by the worker that stores them as chunks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: dict[str, dict] = {}
        self.count = 0

    def add(self, key: str, record: dict) -> None:
        with self._lock:
            self._steps[key] = record
            self.count += 1

    def take(self) -> dict[str, dict]:
        with self._lock:
            steps, self._steps = self._steps, {}
        return steps
