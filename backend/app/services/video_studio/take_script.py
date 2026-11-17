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

"""A recording's script as it was submitted, read back and recorded again.

``record_browser_demo`` keeps the steps and the recording parameters exactly as
they were given (``VideoTake.submitted_script``), once the script passed its
validation and the credential preflight: nothing a step types into a password
field or a stored credential ever reaches it, those scripts are refused before
a take exists. ``get_video_recording`` answers it under ``script``, and
``record_browser_demo(from_recording_id=...)`` records a new take from it, every
argument given overriding what it copies.

A take recorded before the submitted script was kept has none: :func:`readback`
answers ``None`` for it, and :func:`source_script` falls back on its validated
script (``VideoTake.script``, its defaults written out) so it can still be
recorded again.
"""

from __future__ import annotations

from typing import Any

#: The arguments of ``record_browser_demo`` a recording keeps besides its steps, in the order they are said.
RECORDING_PARAMS = (
    "profile_id",
    "start_url",
    "viewport",
    "pixel_ratio",
    "browser_zoom",
    "redact",
    "hide",
    "pace",
    "color_scheme",
    "local_storage",
    "settle_ms",
    "end_hold_ms",
    "record_sign_in",
    "fps",
    "fps_fallback",
    "reset_state",
    "target_render",
    "video_id",
    "scene",
    "insert",
    # Kept only when given: a re-record copies the class its source asked for explicitly, never the one it ran on.
    "worker_class",
)
#: What sizes a take for a screen insert (``target_render`` with its video, scene and insert).
_SIZED_FOR_RENDER = ("target_render", "video_id", "scene", "insert")
#: An explicit size: given on a re-record, it replaces the copied ``target_render`` sizing.
_EXPLICIT_SIZE = ("viewport", "pixel_ratio", "browser_zoom")


def submitted(steps: Any, params: dict[str, Any]) -> dict[str, Any]:
    """What a recording keeps of its submission: *steps* as given, and every parameter of *params* that was given."""
    return {"steps": steps, "params": {key: params[key] for key in RECORDING_PARAMS if params.get(key) is not None}}


def readback(stored: Any) -> dict[str, Any] | None:
    """The ``script`` block of a recording from its stored submission; ``None`` for one recorded before it was kept."""
    if not isinstance(stored, dict) or not isinstance(stored.get("steps"), list):
        return None
    params = stored.get("params") if isinstance(stored.get("params"), dict) else {}
    return {"steps": stored["steps"], "params": dict(params)}


def source_script(stored: Any, script: Any, profile_id: Any) -> dict[str, Any]:
    """The script a re-record copies: the submission kept, else the take's validated *script* and its *profile_id*."""
    kept = readback(stored)
    if kept is not None:
        return kept
    validated = dict(script) if isinstance(script, dict) else {}
    params = {key: value for key, value in validated.items() if key != "steps" and key in RECORDING_PARAMS}
    if profile_id is not None:
        params["profile_id"] = str(profile_id)
    return {"steps": validated.get("steps") or [], "params": params}


def merged(source: dict[str, Any], *, steps: Any, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """``(steps, params)`` of a re-record: *source*'s, every argument given in *steps* and *params* overriding them.

    An explicit viewport, pixel ratio or browser zoom given without a ``target_render`` drops the copied
    ``target_render`` sizing: the size asked is the size recorded.
    """
    copied = dict(source.get("params") or {})
    given = {key: value for key, value in params.items() if key in RECORDING_PARAMS and value is not None}
    if any(key in given for key in _EXPLICIT_SIZE) and "target_render" not in given:
        for key in _SIZED_FOR_RENDER:
            copied.pop(key, None)
    return (source.get("steps") if steps is None else steps), {**copied, **given}
