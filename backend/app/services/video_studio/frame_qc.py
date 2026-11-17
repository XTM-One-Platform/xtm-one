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

"""The frames of a made AI shot, kept and measured.

A shot lands as one video file; what it opened on, where it went and how it
ended were only visible by playing it. When a shot is stored, its first,
middle and last frames are cut and stored as files of the shot
(:func:`frame_file_ids`, ``serialize_clip``'s ``first_frame_file_id`` /
``mid_frame_file_id`` / ``last_frame_file_id``), shown by the tool result and
the inspector, and two numbers are computed from them, in code:

* ``reference_ssim`` - how much the first frame still looks like the start
  picture the shot was asked from: the structural similarity (SSIM) of two
  downscaled grey copies, the picture cut to the frame's shape around its
  centre as a provider fits it. Under the shot's ``min_reference_similarity``
  the take fails as "drifted from reference (ssim 0.41)" (:func:`drift_reason`),
  its video kept on the row to look at, and it is generated again in place.
  With the text check on, ``reference_text_match`` is the share of the
  picture's words the first frame still reads (:func:`text_match`, from the
  text check's own transcription: no second reading);
* ``motion_ssim`` - how much the last frame looks like the first: a locked-off
  shot keeps its frame, a moving camera does not. The save-time quality check
  reads it against the camera the prompt asked for (``quality.content_findings``);
  a shot asked with ``max_camera_motion`` fails when its last frame changed
  more than that from its first (:func:`camera_motion`, :func:`motion_reason`).
  That gate is held to the camera alone: ``camera_motion`` is how far
  the background moved, first frame to last, as a share of the frame
  (``stabilise.camera_of``), so a subject crossing and leaving a locked-off
  frame is no camera move; ``frame_change`` (``1 - motion_ssim``) stays beside
  it, and decides only when the camera could not be followed.
  A take whose camera only drifted is stabilised before it is measured
  (``stabilise``): its frames then keep how (:data:`STABILISATION`),
  and the un-stabilised take as a file of the take.

SSIM follows Wang et al. (2004): a Gaussian window (sigma 1.5), the constants
``(0.01 L)^2`` and ``(0.03 L)^2``, the mean of the map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The params key holding the shot's frames and their measures.
QC_KEY = "frame_qc"
#: The frames cut, by the name of their file id in the params, and where each is taken (``None``: the last).
FRAMES = (("first_frame_file_id", 0.0), ("mid_frame_file_id", 0.5), ("last_frame_file_id", None))
#: The largest side of a frame kept: what the inspector and a chat show.
FRAME_MAX_SIDE = 1280
#: The longest side of the grey copies compared: structure, not noise or compression.
SSIM_SIDE = 256
_SIGMA = 1.5
_K1, _K2, _RANGE = 0.01, 0.03, 255.0


def _grey(picture: Any, size: tuple[int, int]) -> Any:
    import numpy as np
    from PIL import Image

    return np.asarray(picture.convert("L").resize(size, Image.Resampling.BOX), dtype=np.float64)


def cut_box(size: tuple[int, int], aspect: float) -> tuple[int, int, int, int]:
    """The part of a picture of *size* a provider keeps for a frame of *aspect* (width / height): its centre."""
    width, height = size
    if abs(width / height - aspect) < 1e-3:
        return 0, 0, width, height
    if width / height > aspect:
        cut = round(height * aspect)
        left = (width - cut) // 2
        return left, 0, left + cut, height
    cut = round(width / aspect)
    top = (height - cut) // 2
    return 0, top, width, top + cut


def _cut_to(picture: Any, aspect: float) -> Any:
    """*picture* cut around its centre to *aspect* (width / height), as a provider fits a start image."""
    box = cut_box(picture.size, aspect)
    return picture if box == (0, 0, *picture.size) else picture.crop(box)


def ssim(first: Any, second: Any) -> float:
    """The structural similarity of two pictures (Pillow images), from -1 to 1: *second* cut to *first*'s shape."""
    from scipy.ndimage import gaussian_filter

    width, height = first.size
    scale = SSIM_SIDE / max(width, height)
    size = (max(8, round(width * scale)), max(8, round(height * scale)))
    a = _grey(first, size)
    b = _grey(_cut_to(second, width / height), size)
    c1, c2 = (_K1 * _RANGE) ** 2, (_K2 * _RANGE) ** 2
    mu_a, mu_b = gaussian_filter(a, _SIGMA), gaussian_filter(b, _SIGMA)
    var_a = gaussian_filter(a * a, _SIGMA) - mu_a * mu_a
    var_b = gaussian_filter(b * b, _SIGMA) - mu_b * mu_b
    cov = gaussian_filter(a * b, _SIGMA) - mu_a * mu_b
    index = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (var_a + var_b + c2))
    return round(float(index.mean()), 4)


def text_match(picture_text: str, frame_text: str) -> float | None:
    """The share of the picture's words the frame reads (``text_drift.words``); ``None`` for a picture without words."""
    from app.services.video_studio.text_drift import words

    expected = {word.lower() for word in words(picture_text)}
    if not expected:
        return None
    read = {word.lower() for word in words(frame_text)}
    return round(len(expected & read) / len(expected), 3)


#: The params key of how much a take's last frame may change from its first before it fails.
MAX_CAMERA_MOTION = "max_camera_motion"
#: The key, in a take's frames, of the thresholds it passed its checks under: ``{max_camera_motion,
#: min_reference_similarity}``, those applied only. It moves with the take, so the take that plays is judged by
#: them, never by a threshold a later ``generate_video_clip`` call stored on the shot.
CHECKED_WITH = "checked_with"


def checked_with(qc: dict | None) -> dict | None:
    """The thresholds a take passed its checks under; ``None`` for a take made before they were recorded."""
    record = (qc or {}).get(CHECKED_WITH) if isinstance(qc, dict) else None
    return dict(record) if isinstance(record, dict) else None


#: The keys, in a take's frames, of its camera's own motion measured on the background (a share of the
#: frame) and of its whole frame's change (``1 - motion_ssim``).
CAMERA_MOTION = "camera_motion"
FRAME_CHANGE = "frame_change"


def frame_change(qc: dict | None) -> float | None:
    """How much a take's last frame changed from its first: ``1 - motion_ssim``, 0-1; ``None`` when not measured."""
    similarity = (qc or {}).get("motion_ssim")
    if not isinstance(similarity, int | float):
        return None
    return round(min(1.0, max(0.0, 1.0 - float(similarity))), 3)


def camera_motion(qc: dict | None) -> float | None:
    """How far a take's camera moved, what ``max_camera_motion`` is held to; ``None`` when not measured.

    The camera's motion measured on the background when it is known, else the whole frame's change (a take
    whose camera could not be followed, or measured by an older release).
    """
    measured = (qc or {}).get(CAMERA_MOTION)
    if isinstance(measured, int | float):
        return round(min(1.0, max(0.0, float(measured))), 3)
    return frame_change(qc)


def motion_reason(motion: float, tolerance: float, *, change: float | None = None) -> str:
    """The failure of a take whose camera moved more than its ``max_camera_motion``.

    With *change* (the whole frame's ``1 - SSIM``), *motion* is the camera's own, measured on the background;
    without it, the frame's change stood for it.
    """
    if change is None:
        measured = f"its last frame changed {motion:.2f} from its first (1 - SSIM, its camera could not be followed)"
    else:
        measured = (
            f"its camera moved {motion:.2f} of the frame from its first frame to its last (measured on the "
            f"background; the whole frame changed {change:.2f}, 1 - SSIM)"
        )
    return (
        f"camera moved: {measured}, over its max_camera_motion {tolerance:.2f} - generate it again in place "
        "(clip_id) with the camera locked off and less motion, or raise max_camera_motion."
    )


def stabilised_clause(record: dict | None) -> str:
    """What a camera failure adds of the take's stabilisation: done, not done and why, or nothing."""
    if not isinstance(record, dict) or not record:
        return ""
    if record.get("applied"):
        return (
            f" It was stabilised first (motion {float(record.get('raw_motion') or 0):.2f} -> "
            f"{float(record.get('residual_motion') or 0):.2f}): what is left moves in the scene itself, or more than a "
            "camera lock undoes."
        )
    return f" It could not be stabilised: {record.get('reason') or 'no reason given'}."


def drift_reason(similarity: float, threshold: float) -> str:
    """The failure of a take whose first frame drifted from its start picture."""
    return (
        f"drifted from reference (ssim {similarity:.2f}): its first frame is under min_reference_similarity "
        f"{threshold:.2f} - generate it again in place (clip_id) with a calmer camera and a prompt closer to the "
        "start picture, or lower the threshold."
    )


_FRAME_WORDS = {"first_frame_file_id": "first", "mid_frame_file_id": "middle", "last_frame_file_id": "last"}


def caption(name: str, qc: dict, *, shot: str, size: tuple[int, int], duration: float) -> str:
    """What a kept frame is, for a reader who cannot see it: the description its file carries.

    A read of the frame after the model has seen it is answered with it
    (``read_file_content``'s look-once rule) instead of being refused.
    """
    share = dict(FRAMES)[name]
    at = max(duration, 0.0) * (1.0 if share is None else share)
    text = f'The {_FRAME_WORDS[name]} frame ({at:.1f} s) of the AI shot "{shot}", {size[0]}x{size[1]}.'
    if name == "first_frame_file_id" and "reference_ssim" in qc:
        text += f" It keeps {qc['reference_ssim']:.2f} of its start frame's structure (SSIM)."
    if name == "last_frame_file_id" and "motion_ssim" in qc:
        text += f" It keeps {qc['motion_ssim']:.2f} of the first frame's structure (SSIM): lower, the camera or the scene moved."
    return text


#: The key, in a take's frames, of how it was stabilised (``stabilise.Stabilised.as_params``); with
#: ``unstabilised_file_id``, the raw take kept beside the stabilised one, a file of the take like its frames.
STABILISATION = "stabilisation"
UNSTABILISED = "unstabilised_file_id"
#: Its key of each frame's map, stabilised take -> raw take (``stabilise.MAPS``).
STABILISATION_MAPS = "maps"
#: The key, in a take's frames, of the frame its logo was lost in (``logos.LogoLostError``): a file of the take.
LOST_FRAME = "logo_lost_file_id"
#: The key, in a take's frames, of its composited logos' check (``logos.Composited.check``):
#: ``{status: passed | duplicated | not_integrated, frames_sampled, presence, similarity, integration, label?, frame?,
#: seconds?, count?, boxes?, frame_file_id?}`` - what it measured, never a status alone:
#: ``presence`` the frames the logo was drawn in, ``similarity`` the lowest SSIM read back, ``integration``
#: ``logo_integration.Integration.record`` (its frame a file of the take, ``integration.frame_file_id``); the frame a
#: duplicated logo shows in a file of the take. Kept with the frames, like the stabilisation.
LOGO_CHECK = "logo_check"
#: The key, in a take's frames, of the logos drawn onto their subject (``tracked_logos.Tracked.record``):
#: ``{status: drawn, frames, drawn_frames, share, instances, keyframes_sampled, keyframes_seen, labels, subjects,
#: fell_back_from?}``. Kept with the frames, like the stabilisation.
LOGO_TRACKED = "logo_tracked"
#: The key, in a take's frames, of its text check (``text_drift.TextCheck.record``): the words checked, the
#: words the start picture itself holds unreadable, the new text the take shows, the lettering it invented
#: (``invented: [{word, frame, box, crop_file_id}]``, each crop a file of the take) and a failure's word, frame and
#: crop (``crop_file_id``, a file of the take).
TEXT_CHECK = "text_check"
#: The key, in a take's frames, of the flat screen its first frame shows (``clips.take_surface``): its bezel,
#: 0-1, and how flat it is (``surface_refine.Bezel.as_dict``). Measured on the take as made, before a screenshot
#: is drawn over it: carried when the frames are cut again.
SURFACE = "surface"
#: The key of what stands in front of the screen of a shot carrying a screen insert over its take
#: (``insert_keep_out.Overlap.as_dict``): the largest share of the quad, its frame, the mask's confidence. Measured on
#: the take as made, before a screenshot is drawn over it: carried when the frames are cut again.
FOREGROUND_OVERLAP = "foreground_overlap"
#: What a take's frames keep when they are cut again: how it was made, never what is measured on the frames cut.
CARRIED = (STABILISATION, LOGO_CHECK, LOGO_TRACKED, SURFACE, FOREGROUND_OVERLAP)


def _record(qc: dict | None, key: str) -> dict | None:
    record = qc.get(key) if isinstance(qc, dict) else None
    return dict(record) if isinstance(record, dict) else None


def logo_check(qc: dict | None) -> dict | None:
    """A take's duplicate logo check (:data:`LOGO_CHECK`); ``None`` for a take whose logo was not composited, or checked."""
    return _record(qc, LOGO_CHECK)


def integration_score(qc: dict | None) -> float | None:
    """The integration score of the logo drawn onto a take, composited or tracked; ``None`` when none was measured."""
    scores = [
        float(record["integration"]["score"])
        for record in (logo_check(qc), logo_tracked(qc))
        if record is not None and isinstance(record.get("integration"), dict) and record["integration"].get("score") is not None
    ]
    return min(scores) if scores else None


def logo_gate(qc: dict | None) -> dict | None:
    """What a render reads of the logo drawn onto a take: its logo check, its ``integration`` the least integrated of
    the composited and the tracked path's; ``None`` when no logo was drawn onto it."""
    check, tracked = logo_check(qc), logo_tracked(qc)
    if check is None and tracked is None:
        return None
    gate = dict(check or {})
    measured = [
        record["integration"]
        for record in (check, tracked)
        if record is not None and isinstance(record.get("integration"), dict)
    ]
    if measured:
        gate["integration"] = min(measured, key=lambda one: (bool(one.get("passed", True) or one.get("waived")), float(one.get("score") or 0.0)))
    return gate


def integration_frame_ids(qc: dict | None) -> list[str]:
    """The files of the frames a logo was found not integrated in (``integration.frame_file_id``), composited or tracked."""
    return [
        str(record["integration"]["frame_file_id"])
        for record in (logo_check(qc), logo_tracked(qc))
        if record is not None and isinstance(record.get("integration"), dict) and record["integration"].get("frame_file_id")
    ]


def logo_tracked(qc: dict | None) -> dict | None:
    """A take's logos drawn onto their subject (:data:`LOGO_TRACKED`); ``None`` for a take without the tracked path."""
    return _record(qc, LOGO_TRACKED)


def text_check(qc: dict | None) -> dict | None:
    """A take's text check record (:data:`TEXT_CHECK`); ``None`` for a take whose text was not checked."""
    return _record(qc, TEXT_CHECK)


def cut_file_ids(params: dict | None) -> list[str]:
    """The files of a shot's kept frames alone: the three pictures cut from its take."""
    kept = (params or {}).get(QC_KEY)
    if not isinstance(kept, dict):
        return []
    return [str(kept[name]) for name, _at in FRAMES if kept.get(name)]


def video_file_ids(qc: dict | None) -> list[str]:
    """The videos a take's frames keep beside it: its un-stabilised take."""
    record = (qc or {}).get(STABILISATION) if isinstance(qc, dict) else None
    return [str(record[UNSTABILISED])] if isinstance(record, dict) and record.get(UNSTABILISED) else []


def frame_file_ids(params: dict | None) -> list[str]:
    """The files of a shot's kept frames and what they keep beside the take (its own: deleted with it, replaced with its take).

    The frame a logo was lost in is one of them - the frame a tracked
    logo's subject or surface was lost in too -, the frame its
    duplicate check found it twice in, the frame its logo was found
    not integrated in, the crop of a word its
    text check failed on, the crops of the lettering it invented
    (``text_drift.Invented``, dev:invented-text-check) and of each unreadable
    place it counted (``text_drift.Place``, #10919).
    """
    qc = (params or {}).get(QC_KEY)
    lost = [str(qc[LOST_FRAME])] if isinstance(qc, dict) and qc.get(LOST_FRAME) else []
    check = logo_check(qc) or {}
    twice = [str(check["frame_file_id"])] if check.get("frame_file_id") else []
    text = text_check(qc) or {}
    failure = text.get("failure")
    crop = [str(failure["crop_file_id"])] if isinstance(failure, dict) and failure.get("crop_file_id") else []
    invented = [
        str(item["crop_file_id"])
        for item in (text.get("invented") or [])
        if isinstance(item, dict) and item.get("crop_file_id")
    ]
    places = [
        str(item["crop_file_id"])
        for item in (text.get("places") or [])
        if isinstance(item, dict) and item.get("crop_file_id")
    ]
    flat = integration_frame_ids(qc if isinstance(qc, dict) else None)
    return [*cut_file_ids(params), *video_file_ids(qc), *lost, *twice, *flat, *crop, *invented, *places]


def summary(params: dict | None) -> dict:
    """What a shot's frames say, as ``serialize_clip`` serves it: the three file ids, the measures, the stabilisation."""
    kept = (params or {}).get(QC_KEY)
    kept = kept if isinstance(kept, dict) else {}
    record = kept.get(STABILISATION) if isinstance(kept.get(STABILISATION), dict) else None
    return {
        **{name: (str(kept[name]) if kept.get(name) else None) for name, _at in FRAMES},
        "frame_qc": {
            key: kept[key]
            for key in ("reference_ssim", "reference_text_match", "motion_ssim", CAMERA_MOTION, FRAME_CHANGE)
            if key in kept
        }
        or None,
        # The un-stabilised take is the shot's own file: said kept, never named; its frame maps are the render's.
        "stabilisation": (
            {
                **{k: v for k, v in record.items() if k not in (UNSTABILISED, STABILISATION_MAPS)},
                "unstabilised_kept": bool(record.get(UNSTABILISED)),
            }
            if record is not None
            else None
        ),
        "logo_check": logo_check(kept),
        "logo_tracked": logo_tracked(kept),
        "text_check": text_check(kept),
        "surface": _record(kept, SURFACE),
        "foreground_overlap": _record(kept, FOREGROUND_OVERLAP),
    }


async def cut_frames(video_path: str, duration: float, workdir: str) -> dict[str, Path]:
    """The first, middle and last frames of *video_path* as JPEG files, by their params name."""
    from app.services.video_studio import encoder

    cut: dict[str, Path] = {}
    for name, share in FRAMES:
        out = Path(workdir) / f"qc-{name.split('_')[0]}.jpg"
        at = None if share is None else max(duration, 0.0) * share
        await encoder.extract_still(video_path, at=at, out_path=str(out), max_side=FRAME_MAX_SIDE)
        cut[name] = out
    return cut


def open_picture(data: bytes) -> Any:
    import io

    from PIL import Image

    picture = Image.open(io.BytesIO(data))
    picture.load()
    return picture
