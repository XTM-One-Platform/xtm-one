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

"""What a render's screen inserts showed, as findings: over the foreground, padded, a logo
lost, their colours shifted, a surface lost, an insert not drawn at all, off its bezel, keyed with residue,
flickering, soft, haloed, rimmed in grey.

The frame server audits every frame of a matted insert it draws
(``screen_insert.FrameAudit``, kept per segment by ``compositor``); this
module reads those audits back as the render's findings
(``render_checks.Finding``), each with its time in the MP4, its scene and the
frame that shows it:

* ``insert_foreground`` - "insert over foreground": any frame where the
  insert is drawn - past ``screen_insert.DRAWN_MIN_ALPHA``, seen through
  included - over what its matte says stands in front of the surface, its
  edge ring left out, on more than ``screen_insert.MAX_COVERED_SHARE`` of its
  quad (0.5 %), with that frame and its worst stretch at 100 %
  (``foreground_crop``);
* ``insert_through_foreground`` - any frame where the source's own
  strokes show across the core of what stands in front, read in the frame's
  pixels (``screen_insert.through_share``), on more than
  ``screen_insert.MAX_THROUGH_SHARE`` of it, with that frame and its worst
  stretch at 100 % (``through_crop``);
* ``insert_ghost`` - the mask under
  ``screen_insert.LOW_FRONT_CONFIDENCE`` while what stands in front covers
  more than ``screen_insert.GHOST_MIN_FOREGROUND`` of the quad, on more than
  :data:`GHOST_FRAME_SHARE` of the insert's frames: one action, a keyed
  surface or a subject far from the key's colours; above, a fact;
* ``key_spill_outside_quad`` - a keyed insert's key hue past its quad
  on the whole footage frame (``screen_insert.key_spill``), the whole frame
  kept;
* ``insert_padded`` - a picture drawn with a ``contain`` fit whose own pixels
  hold flat bands on two opposite sides, in a colour that is not its border
  on the other two (a letterbox, a card baked into it;
  ``screen_insert.padded_sides``);
* ``logo_lost`` - a brand mark inserted (``source.asset``) showing less than
  ``screen_insert.LOGO_MIN_VISIBLE`` of its pixels in any frame;
* ``insert_colour_shift`` - a frame where the quad as composited,
  held to its source drawn into the same pixels
  (``screen_insert.colour_fidelity``), has its blacks lifted past
  ``screen_insert.MAX_BLACK_LIFT``, its hue turned past
  ``MAX_HUE_ROTATION_DEGREES`` or, on a source with no hue of its own, a cast
  past ``MAX_CAST``: "tinted blue, blacks lifted 18%", with its luminance,
  saturation and contrast, and its one action (its ``light`` lowered, or what
  shows through it) - read clear of what stands in front, never on a frame
  it covers more than ``screen_insert.COLOUR_MAX_FOREGROUND`` of the quad,
  on :data:`MIN_FAILING_FRAMES` frames;
* ``insert_flatness`` - a surface the frames show curved: an edge
  bowing past ``surface_refine.FLAT_MAX_SHARE`` on its visible part, its
  points confirming it (:func:`frame_flatness`: too few on one line
  AND standing off it), five frames in a row; an edge mostly covered by what
  stands in front is not judged, said; its quad off the bezel and the key
  left past its edge follow from it, folded into it; a surface seen at an
  angle is no finding, nor a bow the insert drawn on it covers - on
  its bezel, no key left past it (:func:`covers_surface`), a fact; a static
  insert the save measured takes the save's verdict, on its footage frame,
  never its drawn frames' (*measured*, :func:`findings`);
* ``insert_missing`` - an insert the render cannot draw at all (a brand mark
  the template has no file for, an SVG the rasteriser drew nothing of,
  :func:`missing`): the audience would see what the footage's model drew;
* ``surface_lost`` - a tracked surface the track lost (``insert_tracking``),
  the render stopped before it drew a frame;
* ``insert_alignment`` - frames where the given quad is more than
  ``surface_refine.ALIGNMENT_MAX_SHARE`` of the frame's width off the bezel
  found under it - its mean or one side - (the insert is drawn
  snapped to the bezel, the author is told the quad to give), five in a row
  (``surface_refine.alignment_failing``); on a join frame
  (:func:`bezel_readings`), a neighbour's insert in a scene preview is read
  only at the size it was last measured at - else its measure is a fact -,
  and a reading drawn smaller than the footage the save measured and off the
  save's measure is set aside for it, said to the administrator;
* ``insert_jitter`` - a frame whose drawn quad (``FrameAudit.refined``)
  jumps against the frame before it beyond the motion of the frames around
  it by more than :data:`JITTER_MAX_SHARE` of the frame's width
  (:func:`quad_jitter`): a blinking screen the mean alignment averages away,
  with both frames' region side by side (:func:`jitter_proofs`) and the
  worst jump in the insert's report (``jitter_max``);
* ``insert_residual_key`` - a frame where connected regions of the key's hue
  remain on more than ``screen_insert.RESIDUAL_FAIL_SHARE`` of the quad,
  inside it or within ``screen_insert.RESIDUAL_MARGIN`` of it after
  compositing (under the share, the report says it; past the margin,
  the footage's own, despilled), said by where it sits (inside the quad, the
  bands a contain fit leaves in it, the surface running past it, or
  the footage's key colour past the bezel the key did not take) and on which
  side, with the action where most of it sits - the source's own key hue, the
  filled contain bands and what stands in front left out
  (``screen_insert.GateMasks``), on :data:`MIN_FAILING_FRAMES` frames -
  or on one frame of its scene's joins with the scenes beside it
  (``Placed.joins``: a transition's frames, a cut's frames each side), where
  the key shows through and no later frame hides it;
* ``insert_flicker`` - the matte's coverage of the quad, or the insert's luma
  against its source frame, reversing from one frame to the next by more
  than ``screen_insert.FLICKER_COVERAGE`` / ``FLICKER_LUMA``
  (:func:`flicker_frames`), or more than one less
  ``screens.MIN_EDGE_HOLD`` of the matte as drawn toggling back over the last
  two frames (``edge_hold``); or the insert drawn on one frame and not
  on the next one the page draws it on (:func:`missing_frames`): a keyed
  insert is drawn on every frame of its scene, both transitions included.
  Both judged (:func:`owned_flicker`) on the frames that are
  its scene's - the presence gate's too (``Placed.owns_frame``): those its
  scene owns alone, and those of its transitions where the page shows its
  quad at ``PRESENCE_MIN_SHOWN`` or more, never the first frame of its
  transition in (the scene before's). Its layer not drawn is raised
  by the presence gate alone, on those frames it reads the insert missing on
  off the encoded frame shown whole, never a transition's: a frame the page
  log misses and the encoded frame shows drawn is no finding. Its report's
  flicker counts both, so a note of either never stands beside a flicker of
  0 (:func:`contradictions`);
* ``insert_dropped`` - a keyed insert's key read on the encoded frames
  of its scene's joins (:func:`guard_frames`, :func:`dropped`): the render's
  own output, never the frame server's account of it;
* ``insert_soft`` - a frame whose insert region's Laplacian variance at the
  output size is under ``screen_insert.MIN_SHARPNESS`` of the source's, or
  (:func:`soft`, :func:`soft_screens`) a source with fewer than
  ``screen_insert.MIN_DENSITY`` of its own pixels per pixel of its quad at the
  render's size, the source and the size it needs named - a recording's pixel
  ratio too; a replaced screen's screenshot the same way. Before any render,
  :func:`soft_by_quality` reads the gate's measure at every quality a
  whole video may be rendered at, what a save and a scene preview advise,
  :func:`recording_for` sizes a recording for the quads it will be drawn on,
  and :func:`soft_takes` names the recordings a save refuses: soft at
  the quality the video is made for, each with the recording that passes;
* ``insert_halo`` - frames with hue outliers (the key's hue, its complement)
  along more than ``screen_insert.MAX_HALO_SHARE`` of the foreground's edge,
  on :data:`HALO_MIN_FRAMES` of them at least, the subject's own
  colour left out and said (its hues over its clip, a hue
  running on into it, the complement wherever the compositor's clamp did not
  make it), what was counted where the clamp touched said apart, and
  the edge over the source's own key hue; measured after the edge
  despill and said as the edge spill it leaves at its width, its worst
  stretch kept at 100 %, its action the despill widened - or turned on -
  before the shot is made again, or, no setting moving it, the
  subject's own colour declared (``despill.own_hues``) or accepted;
* ``insert_rim`` - frames where more than
  ``screen_insert.MAX_RIM_SHARE`` of the band ``screen_insert.RIM_FROM_PX``
  to ``RIM_PX`` on the surface side of what stands in front - of its part
  the key reads clear - is grey where the insert is not, its luma between
  the subject's and the insert's (``FrameAudit.rim``): the key's residue a
  dark edge leaves, which no hue gate reads; on :data:`MIN_FAILING_FRAMES`
  of them, its worst stretch kept at 100 %, its action the insert's light
  lowered when that light made most of it grey, else the shot made again;
* ``insert_cropped`` - a frame where a ``cover`` fit cuts more than
  ``screen_insert.MAX_CONTENT_CUT`` of the source's content (a sidebar, a
  right column) to fill the quad's shape;
* ``insert_band_key_colour`` - an unkeyed ``contain`` insert whose bands, left
  to the footage, show a chroma key colour on most of its frames
  (``FrameAudit.band_key``): the fix is the key set to that colour, the bands
  then filled with the bezel's colour, or ``fit: cover``.

A finding that an earlier drawing differing in its key, feather or light
alone measured the same to three decimals says ``not a setting:`` and what
leaves it instead, one of :data:`NOT_A_SETTING_CAUSES`
(:func:`unchanged`).

Every finding of a frame-by-frame gate carries its gate, the value against
its threshold, the mask shares left out of what it judged on its worst frame
(:func:`excluded_shares`) and exactly one action (:data:`ACTIONS`,
``data["action"]``, its ``fix``): a setting is the action only where the
fixtures show the measurement responds to it - the light, on blacks the
footage's light lifts -, never a key's tolerance or feather. The
alignment and the flatness fail on :data:`surface_refine.PERSIST_FRAMES`
consecutive frames, the residual key, the halo, the grey rim and the
colours on :data:`MIN_FAILING_FRAMES` frames. What each gate concluded is kept
on the insert's report (``verdicts``): a later drawing of the same shot and
spec - a save's measure, a preview - that passed a gate the render then
fails makes that finding a warning that ``disagrees with`` it, never a block
(*passed*, :func:`findings`).

Each is a failure: a whole render with one fails, its MP4 kept to look at;
``render_video_scene`` lists it on its scene, and the gate refuses a render of
the revision until it is fixed (``render_checks.PICTURE_KINDS``). The frame a
finding names is stored as a file of the render (``data.frame_file_id``),
deleted with it (``render_retention.render_files``). :func:`reports` reads
the same audits as what each insert measured - its refined quad, mean seed
error, residual key, flicker, sharpness, halo, its colours at its light -
with the 100 % crops of its region, for the render's
``checks.inserts`` and the tool answers, and the resolution both come from
(the frames the numbers were measured on, the frame the crops were
cut from - a 2160p render's are 2160p, a preview's its draft's).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Collection, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio import screen_insert, screens
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.pictures import quad_size
from app.services.video_studio.render_checks import (
    FAIL,
    HARNESS_MAX_FRAMES,
    INSERT_ALIGNMENT,
    INSERT_BAND_KEY_COLOUR,
    INSERT_COLOUR_SHIFT,
    INSERT_CROPPED,
    INSERT_DROPPED,
    INSERT_FLATNESS,
    INSERT_FLICKER,
    INSERT_FOREGROUND,
    INSERT_GAP,
    INSERT_GHOST,
    INSERT_HALO,
    INSERT_JITTER,
    INSERT_MATTE_HALO,
    INSERT_MATTE_LAG,
    INSERT_MEASUREMENT_LOST,
    INSERT_MISSING,
    INSERT_OCCLUDED,
    INSERT_PADDED,
    INSERT_RESIDUAL_KEY,
    INSERT_RIM,
    INSERT_SOFT,
    INSERT_THROUGH_FOREGROUND,
    KEY_SPILL_OUTSIDE_QUAD,
    KIND_FIXES,
    LOGO_LOST,
    SAME_FRAMES_SECONDS,
    SURFACE_LOST,
    UNEXPLAINED_ACTION,
    WARN,
    Finding,
    clock,
    findings_of,
    harness_of,
    insert_reports,
)
from app.services.video_studio.spec import INSERT_DEFAULT_LIGHT, INSERT_FADE_SECONDS
from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

# ── What a finding recommends, and what it left out ──
#: The one action a screen insert's finding recommends (``data["action"]["kind"]``): the shot made again with a flat
#: screen (a confirmed curvature), the shot made again (what the footage itself holds: a flicker, its own key colour
#: past the bezel, a halo no despill setting moved), a recording made again at another viewport (its text too small),
#: the quad's corners given (off the bezel, or the surface running past it), another source (too small, translucent,
#: of other proportions), the light lowered (blacks the footage's light lifts), the edge despill widened or turned on
#: (a halo left on the edge of what stands in front) - the two settings the fixtures show move their
#: measurement -, the subject's own colour declared (a halo no despill setting moved, ``despill.own_hues``), the
#: insert kept off the transition its missing frames all lie in (:func:`join_action`), or nothing (by design,
#: its reason said).
REGENERATE_FLAT, REGENERATE_SHOT, RERECORD_VIEWPORT = "regenerate_flat_screen", "regenerate_shot", "rerecord_viewport"
GIVE_QUAD, REPLACE_SOURCE, LOWER_LIGHT, NOTHING = "give_quad_corners", "replace_source", "lower_light", UNEXPLAINED_ACTION
WIDEN_DESPILL, DECLARE_OWN_HUES, AVOID_TRANSITION = "widen_despill", "declare_own_hues", "avoid_transition"
#: Two inserts back to back on one surface leaving it bare between them (:func:`insert_gaps`): overlap them.
OVERLAP_INSERTS = "overlap_inserts"
#: An unkeyed contain insert whose bands show the screen's key colour (:func:`_band_key_findings`): key it.
SET_KEY = "set_key"
ACTIONS = (
    REGENERATE_FLAT,
    REGENERATE_SHOT,
    RERECORD_VIEWPORT,
    GIVE_QUAD,
    REPLACE_SOURCE,
    LOWER_LIGHT,
    WIDEN_DESPILL,
    DECLARE_OWN_HUES,
    AVOID_TRANSITION,
    OVERLAP_INSERTS,
    SET_KEY,
    NOTHING,
)
#: What leaves a finding no key, feather or light setting moved between two drawings (:func:`unchanged`).
SOURCE_CONTENT, FOREGROUND, PAST_BEZEL, PERSPECTIVE, CURVATURE, OWN_COLOUR = (
    "source content",
    "foreground",
    "footage past bezel",
    "perspective",
    "curvature",
    "the foreground's own colour",
)
NOT_A_SETTING_CAUSES = (SOURCE_CONTENT, FOREGROUND, PAST_BEZEL, PERSPECTIVE, CURVATURE, OWN_COLOUR)
#: The gate each frame-by-frame finding is: what its report's ``verdicts`` and a later drawing are keyed by.
GATE_OF = {
    INSERT_ALIGNMENT: "alignment",
    INSERT_JITTER: "jitter",
    INSERT_FLATNESS: "flatness",
    INSERT_RESIDUAL_KEY: "residual_key",
    INSERT_HALO: "halo",
    INSERT_RIM: "rim",
    INSERT_COLOUR_SHIFT: "colour_shift",
    INSERT_SOFT: "sharpness",
    INSERT_FLICKER: "flicker",
}
#: A residual key, a halo, a grey rim or a colour shift fails on at least this many frames past its threshold -
#: or on every frame of an insert drawn on fewer: a strand crossing the edge, a frame of noise is never one.
MIN_FAILING_FRAMES = 5
#: An insert whose mask of what stands in front is a ghost (``screen_insert.FrameAudit.ghosted``) on more than this share
#: of its frames fails (``insert_ghost``): a frame or two of doubt is said in its report, never a finding.
GHOST_FRAME_SHARE = 0.05
#: ... or on this many consecutive frames, whatever their share (dev:video-render-wide-insert-gate): a matte that flickers to
#: a ghost for a third of a second in a long insert is seen, though it stays under :data:`GHOST_FRAME_SHARE`.
GHOST_RUN_FRAMES = 6
#: An insert whose mask lags its subject (``screen_insert.FrameAudit.matte_lags``) on more than this share of its frames
#: fails (``insert_matte_lag``).
MATTE_LAG_FRAME_SHARE = 0.05
#: A legible insert hidden past ``screen_insert.OCCLUDED_SHARE`` of its quad on more than this share of its frames - or
#: past ``screen_insert.OCCLUDED_PEAK`` on any - fails (``insert_occluded``).
OCCLUDED_FRAME_SHARE = 0.10


def action(kind: str, text: str) -> dict:
    """``{kind, text}``: the one action a finding recommends (:data:`ACTIONS`)."""
    if kind not in ACTIONS:
        raise ValueError(f"not an action: {kind}")
    return {"kind": kind, "text": text}


def enough(failing: list[int], frames: int) -> bool:
    """Whether a gate failing on the frames *failing* of an insert's *frames* drawn fails it (:data:`MIN_FAILING_FRAMES`)."""
    return bool(failing) and len(failing) >= min(MIN_FAILING_FRAMES, max(int(frames), 1))


def excluded_shares(audit: dict) -> dict[str, float]:
    """The mask shares a frame's gates left out of what they judged (``screen_insert.GateMasks``): the source's
    own key hue, what stands in front (``coverage``) and the contain bands, each a share of the quad, those measured."""
    out = {}
    for name, field in (("source_keyhue", "source_key_hue"), ("coverage", "foreground_share"), ("contain_band", "band_share")):
        if audit.get(field) is not None:
            out[name] = round(float(audit[field]), 4)
    return out


def _gate_data(gate: str, audit: dict, value: float, threshold: float, chosen: dict) -> dict:
    """What every frame-by-frame finding carries besides its own: its gate, its value against its threshold,
    the mask shares left out on its worst frame and its one action."""
    return {"gate": gate, "value": round(float(value), 5), "threshold": threshold, "excluded": excluded_shares(audit), "action": chosen}


@dataclass(frozen=True)
class InsertTiming:
    """A screen insert's overlay fields a re-seed changes (``spec.Overlay``): its ``start`` and ``duration`` in its
    scene, its source's ``trim_start``, and what keeps a split from being drawn as two copies of it - a wall pane
    (``pane``), a time read from a step (``at_step``), a push ending at ``zoom_end`` seconds in."""

    start: float
    duration: float
    trim_start: float | None = None
    pane: int | None = None
    at_step: int | None = None
    zoom_end: float | None = None
    #: What its source is (``file`` / ``take`` / ``clip`` / ``asset``): a brand mark has no time to skip.
    kind: str | None = None

    @classmethod
    def of(cls, part: Any) -> InsertTiming | None:
        """The timing of *part* (``timeline.InsertPart``), or ``None`` when it carries no overlay."""
        overlay = getattr(part, "overlay", None)
        if overlay is None or getattr(overlay, "duration", None) is None:
            return None
        zoom = getattr(overlay, "zoom", None)
        trim = getattr(overlay, "trim_start", None)
        return cls(
            float(getattr(overlay, "start", 0.0) or 0.0),
            float(overlay.duration),
            float(trim) if trim is not None else None,
            getattr(part, "pane", None),
            getattr(overlay, "at_step", None),
            float(zoom.end) if zoom is not None else None,
            getattr(part, "kind", None),
        )


@dataclass(frozen=True)
class Placed:
    """A screen insert as the plan places it: its scene, when it shows in the video, what it is."""

    key: str
    scene_id: str | None
    label: str
    start: float
    end: float
    padded: str | None = None
    logo: str | None = None
    #: Its source's pixels per pixel of its quad at the render's size (``screen_insert.Density.as_dict``).
    soft: dict | None = None
    #: What its source is, for the finding that names it: ``take`` / ``clip`` / ``file`` / ``asset``, and its id.
    source: tuple[str, str] | None = None
    #: The light it is drawn at and whether it is keyed: what its colour finding tells to change.
    light: float | None = None
    keyed: bool = False
    #: Its fit, and its key as the author set it (:func:`key_settings`): what tells a residual key no key
    #: setting moves between two previews.
    fit: str | None = None
    key_settings: dict | None = None
    #: ``(from, to)`` in the video where its scene is on screen alone (``timeline.owned_span``): its crops are
    #: cut there, never in a transition that blends it with a neighbour.
    owned: tuple[float, float] | None = None
    #: The digest of every other field it is drawn by - its source, quad, fit, glow, trim (:func:`rest_digest`):
    #: two drawings with the same one differ in their key, their light or their edge despill alone.
    drawn: str | None = None
    #: A keyed insert's edge despill as it is drawn (``screen_insert.Despill.as_dict``): what its halo finding
    #: says it was measured after, and the setting it advises.
    despill: dict | None = None
    #: The output frames the page draws its layer on (:func:`drawn_frames`), as ``[first, end)`` runs: a matted
    #: insert's, each of which the frame server audits - a keyed one held on its surface over its whole scene. Empty
    #: for an insert nothing audits.
    expected: tuple[tuple[int, int], ...] = ()
    #: The output frames ``[first, end)`` of its scene's joins with the scenes beside it
    #: (``frame_owners.FrameOwners.joins``): a transition's frames, a cut's frames each side of it.
    joins: tuple[tuple[int, int], ...] = ()
    #: Whether it runs to its scene's end (``timeline.run_to_end``): it never fades out.
    to_end: bool = False
    #: The output frames ``[first, end)`` its scene is painted on where it shows whole (:func:`shown_whole`):
    #: where the presence gate reads its source off the encoded frames.
    whole: tuple[tuple[int, int], ...] = ()
    #: The output frames ``[first, end)`` a transition or the video's fade dims or blurs its whole scene on, each with
    #: what it is (``blur in``, ``fade_black out``, ``video fade in``): its scene's transitions that last, the video's
    #: fades from and to black (:func:`blend_windows`). The presence gate judges its quad there against the frame
    #: around it.
    blends: tuple[tuple[int, int, str], ...] = ()
    #: The output frames ``[first, end)`` its scene owns in the render's ownership table
    #: (``frame_owners.FrameOwners.owned``): the first frame of its transition in is the scene before's, its transition
    #: out's other frames the scene coming in's. ``None``: not planned.
    owns: tuple[int, int] | None = None
    #: The output frames ``[first, end)`` its scene owns alone (``frame_owners.FrameOwners.alone``): past its
    #: transition in, the page's progress 1 there. ``None``: not planned.
    alone: tuple[int, int] | None = None
    #: Whether it is there to be read (``spec.INSERT_LEGIBILITY``): an ``ambient`` one is never
    #: ``insert_occluded``.
    legible: bool = True
    #: Whether its quad is followed frame by frame (``track: true``): an alignment finding never advises the setting
    #: the author already chose (:func:`_alignment_remedy`).
    tracked: bool = False
    #: Its overlay as the spec gives it (:class:`InsertTiming`): what a tracked insert's split names field by field.
    #: ``None``: not read from a spec.
    timing: InsertTiming | None = None

    def at_join(self, frame: int) -> bool:
        """Whether output *frame* is one of its scene's joins with another (:attr:`joins`)."""
        return any(first <= frame < end for first, end in self.joins)

    def blend_at(self, frame: int) -> tuple[int, int, str] | None:
        """The window of :attr:`blends` output *frame* is in, or ``None``."""
        return next((window for window in self.blends if window[0] <= frame < window[1]), None)

    def expected_frames(self, frame_range: tuple[int, int] | None = None) -> list[int]:
        """The output frames its layer is drawn on (:attr:`expected`), within *frame_range* (a scene preview's) when given."""
        low, high = frame_range if frame_range is not None else (0, None)
        return [
            frame
            for first, end in self.expected
            for frame in range(max(first, low), end if high is None else min(end, high))
        ]

    def whole_frames(self, frame_range: tuple[int, int] | None = None) -> list[int]:
        """The output frames it shows whole on (:attr:`whole`), within *frame_range* when given."""
        low, high = frame_range if frame_range is not None else (0, None)
        return [frame for first, end in self.whole for frame in range(max(first, low), end if high is None else min(end, high))]

    def owns_frame(self, frame: int, placements: dict[int, Any] | None = None) -> bool:
        """Whether output *frame* is its scene's for its checks: the one table its flicker, its flicker fact and
        the presence gate read. A frame its scene owns alone (:attr:`alone`) is; a frame of its transition in or out
        is where the page placed its quad at :data:`PRESENCE_MIN_SHOWN` or more there (*placements*: ``frame -> [x0,
        y0, x1, y1, shown]``, :func:`placed_share`); the first frame of its transition in never is - the scene before's
        (``frame_owners.FrameOwners.owner``). Every frame of an insert not planned is."""
        if self.owns is None:
            return True
        if self.alone is not None and self.alone[0] <= frame < self.alone[1]:
            return True
        if frame < self.owns[0]:
            return False
        spot = (placements or {}).get(frame)
        return spot is not None and placed_share(spot) >= PRESENCE_MIN_SHOWN

    def judged_frames(self, placements: dict[int, Any] | None = None, frame_range: tuple[int, int] | None = None) -> list[int]:
        """The output frames its layer is judged drawn on (:func:`missing_frames`): those the page draws it
        on (:attr:`expected`) that are its scene's (:meth:`owns_frame`), within *frame_range* when given."""
        return [frame for frame in self.expected_frames(frame_range) if self.owns_frame(frame, placements)]


def placed_share(spot: Any) -> float:
    """How much of an insert the page showed where it placed it on a frame (``compositor.FrameServer.place``: ``[x0, y0,
    x1, y1, shown]``, the page's ``insertRects``) - whole for a placement kept without it."""
    return float(spot[4]) if len(spot) > 4 else 1.0


def placements_of(part: dict) -> dict[int, Any]:
    """``output frame -> [x0, y0, x1, y1, shown]`` where the page placed an insert (its audits' ``placed``, *part*)."""
    return {int(n): box for n, box in (part.get("placed") or {}).items()}


def _painted(document: dict, index: int, owners: FrameOwners | None = None) -> Iterator[tuple[int, float]]:
    """``(output frame, seconds into the scene)`` of every frame the page paints the scene at *index* of a planned
    *document* on: its encoded frames (*owners*, the document's ``frame_owners.FrameOwners`` by default)."""
    owners = owners if owners is not None else FrameOwners.of(document)
    start = float(list(document.get("scenes") or [])[index]["start"])
    for frame in owners.frames_of(index):
        yield frame, max(0.0, frame / owners.fps - start)


def drawn_frames(document: dict, index: int, owners: FrameOwners | None = None) -> dict[str, list[int]]:
    """``insert key -> the output frames the page draws its layer on`` for the scene at *index* of a planned *document*:
    every frame the page shows the scene on (:func:`_painted`), where ``timeline.insert_layers`` gives the
    insert an opacity - a keyed insert held on its surface on each of them."""
    from app.services.video_studio.timeline import insert_layers

    scene = list(document.get("scenes") or [])[index]
    out: dict[str, list[int]] = {}
    for frame, t in _painted(document, index, owners):
        for payload, opacity, _shown in insert_layers(scene, t):
            if opacity > 0:
                out.setdefault(str(payload.get("src")), []).append(frame)
    return out


def shown_whole(document: dict, index: int, owners: FrameOwners | None = None) -> dict[str, list[int]]:
    """``insert key -> the output frames it shows whole on`` for the scene at *index* of a planned *document*:
    every frame the page paints the scene on where ``timeline.insert_visibility`` is 1 - to its scene's last encoded
    frame for one that runs to its end, never a frame the next scene owns."""
    from app.services.video_studio.timeline import insert_visibility

    scene = list(document.get("scenes") or [])[index]
    inserts = [payload for payload in scene.get("inserts") or [] if isinstance(payload, dict)]
    out: dict[str, list[int]] = {}
    for frame, t in _painted(document, index, owners):
        for payload in inserts:
            if insert_visibility(payload, t) >= 1.0:
                out.setdefault(str(payload.get("src")), []).append(frame)
    return out


def blend_windows(document: dict, index: int, owners: FrameOwners | None = None) -> tuple[tuple[int, int, str], ...]:
    """The output frames ``[first, end)`` a transition or a fade dims or blurs the scene at *index* of a planned
    *document* on (:attr:`Placed.blends`): each transition into or out of it that lasts - the frames both its
    scenes are painted on (``frame_owners.FrameOwners.joins``) -, named by its type and side, and the video's
    fades from and to black."""
    owners = owners if owners is not None else FrameOwners.of(document)
    rate = owners.fps
    scenes = list(document.get("scenes") or [])
    out = []
    for first, end, coming in owners.joins():
        if coming not in (index, index + 1) or not owners.blended[coming]:
            continue
        transition = scenes[coming].get("transition_in") or {}
        side = "in" if coming == index else "out"
        out.append((first, end, f"{transition.get('type') or 'crossfade'} {side}"))
    duration = float(document.get("duration") or 0.0)
    if float(document.get("fade_in") or 0.0) > 0:
        out.append((0, math.ceil(float(document["fade_in"]) * rate), "video fade in"))
    if float(document.get("fade_out") or 0.0) > 0:
        out.append((math.floor((duration - float(document["fade_out"])) * rate), owners.total, "video fade out"))
    return tuple(out)


def join_action(document: dict, where: Placed, frames: list[int], *, fps: float) -> dict | None:
    """The spec change that keeps an insert off the output *frames* a finding names: when they all lie in one
    transition of its scene (:attr:`Placed.blends`), that transition made a cut, or the insert ended before it (a
    transition out) or started after it (a transition in) - ``None`` when they do not, or lie in the video's own fade:
    then nothing in the spec moves them, and the finding is the render harness's (``render_checks.harness_defects``)."""
    windows = {where.blend_at(frame) for frame in frames}
    if len(windows) != 1 or None in windows:
        return None
    first, end, what = next(iter(windows))
    kind, _, side = what.rpartition(" ")
    scenes = list(document.get("scenes") or [])
    index = next((i for i, scene in enumerate(scenes) if scene.get("id") == where.scene_id), None)
    if what.startswith("video ") or index is None or (side == "out" and index + 1 >= len(scenes)):
        return None
    rate = max(float(fps), 1.0)
    begins, ends = first / rate, end / rate
    if side == "out":
        coming = scenes[index + 1].get("id")
        text = (
            f"its frames are the {kind} out of scene {where.scene_id} into scene {coming} ({clock(begins)} to "
            f"{clock(ends)}): make scene {coming}'s transition a cut, or end the insert before it (a duration under "
            f"{max(begins - where.start, 0.0):.2f} s)"
        )
    else:
        start = float(scenes[index].get("start") or 0.0)
        text = (
            f"its frames are the {kind} into scene {where.scene_id} ({clock(begins)} to {clock(ends)}): make scene "
            f"{where.scene_id}'s transition a cut, or start the insert after it (a start of {ends - start:.2f} s or more)"
        )
    return action(AVOID_TRANSITION, text)


def rest_digest(part: Any) -> str:
    """What a screen insert (*part*, ``timeline.InsertPart``) is drawn by, but its key, its light and its edge despill:
    its source, its quad, its pane and every other field of its overlay, hashed."""
    overlay = part.overlay
    fields = overlay.model_dump(mode="json", exclude={"key", "light", "despill"}) if hasattr(overlay, "model_dump") else repr(overlay)
    shown = {"source": [str(part.kind), str(part.ref)], "quad": [list(point) for point in part.quad], "pane": part.pane, "overlay": fields}
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def key_settings(key: Any) -> dict | None:
    """A screen insert's key as the author set it (``screen_insert.KeySpec``): ``{color, tolerance, feather}``; ``None``."""
    if key is None:
        return None
    return {"color": str(key.color), "tolerance": round(float(key.tolerance), 4), "feather": round(float(key.feather), 4)}


def placed_inserts(
    document: dict, inserts: dict[str, Any], sources: dict[str, Any] | None = None, owners: FrameOwners | None = None
) -> dict[str, Placed]:
    """Every screen insert of the planned *document*, by key; *inserts* are the render's sources (their labels, marks).

    *sources* (``timeline.insert_sources``, optional) say what each insert
    shows - a recording, a shot, a file, a brand mark - for the finding that
    names it. The frames each one is judged on are its scene's encoded frames
    (*owners*: the render's ``frame_owners.FrameOwners``, the
    document's by default), its joins those of the table too, and the frames
    its checks judge it on those its scene owns there.
    """
    from app.services.video_studio.timeline import owned_span

    placed: dict[str, Placed] = {}
    planned = "duration" in document
    owners = (owners if owners is not None else FrameOwners.of(document)) if planned else None
    joined = owners.joins() if owners is not None else []
    for index, scene in enumerate(document.get("scenes") or []):
        start = float(scene.get("start") or 0.0)
        owned = owned_span(document, index) if planned else None
        drawn = drawn_frames(document, index, owners) if owners is not None and scene.get("inserts") else {}
        whole = shown_whole(document, index, owners) if owners is not None and scene.get("inserts") else {}
        joins = tuple((first, end) for first, end, coming in joined if coming in (index, index + 1))
        blends = blend_windows(document, index, owners) if owners is not None else ()
        owns = alone = None
        if owners is not None:
            mine, solo = owners.owned(index), owners.alone(index)
            owns, alone = (mine.start, mine.stop), (solo.start, solo.stop)
        for payload in scene.get("inserts") or []:
            key = payload.get("src")
            source = inserts.get(key)
            if not key or source is None:
                continue
            part = (sources or {}).get(key)
            audited = getattr(source, "footage", None) is not None
            placed[key] = Placed(
                key,
                scene.get("id"),
                source.label or f"Scene {scene.get('id')}: a screen insert",
                start + float(payload.get("t0") or 0.0),
                start + float(payload.get("t1") or 0.0),
                payload.get("padded"),
                source.logo,
                payload.get("soft") if isinstance(payload.get("soft"), dict) else None,
                (str(part.kind), str(part.ref)) if part is not None else None,
                getattr(source, "light", None),
                getattr(source, "key", None) is not None,
                getattr(source, "fit", None),
                key_settings(getattr(source, "key", None)),
                owned,
                rest_digest(part) if part is not None else None,
                source.despill.as_dict() if getattr(source, "key", None) is not None else None,
                tuple((run[0], run[1] + 1) for run in _runs(drawn.get(key, []))) if audited else (),
                joins,
                bool(payload.get("to_end")),
                tuple((run[0], run[1] + 1) for run in _runs(whole.get(key, []))),
                blends,
                owns,
                alone,
                not _ambient(part),
                bool(payload.get("track")) or getattr(source, "track", None) is not None,
                timing=InsertTiming.of(part),
            )
    return placed


def _ambient(part: Any) -> bool:
    """Whether *part* (``timeline.InsertPart``) is a screen insert declared scenery (``legibility: ambient``)."""
    from app.services.video_studio.insert_legibility import ambient

    return part is not None and ambient(getattr(part, "overlay", None))


_CROPPED_FIX = "use fit: contain, or a quad of the source's proportions"


def band_key_fix(colour: str) -> str:
    """What an unkeyed contain insert whose bands show the key colour *colour* changes (:func:`_band_key_findings`)."""
    return f"set key: {colour} on that insert (its contain bands are then filled with the bezel's colour), or use fit: cover"


def _band_key_findings(where: Placed, frames: dict[int, dict], at: Any) -> list[tuple[Finding, str | None]]:
    """``insert_band_key_colour`` for *where* when its contain bands, left to the footage (no key), show a chroma key
    colour on most of its audited *frames* (``FrameAudit.band_key``): the screen's own green, two bars a viewer sees,
    never the fit's design."""
    band = _band_report(frames)
    if band is None or not band.get("key"):
        return []
    colour = str(band["key"])
    hit = sorted(n for n, audit in frames.items() if audit.get("band_key") == colour)
    fix = band_key_fix(colour)
    message = (
        f"key colour in the contain bands at t={clock(at(hit[0]))}: {where.label} has fit: contain and no key, and its "
        f"bands ({float(band['share']):.1%} of the quad) show the footage's {colour} screen on "
        f"{float(band.get('key_share') or 0.0):.0%} of their pixels ({len(hit)} frame{'s' if len(hit) != 1 else ''}): {fix}"
    )
    data = {
        "insert": where.key,
        "frames": len(hit),
        "band_share": band["share"],
        "band_key": colour,
        "band_key_share": band.get("key_share"),
        "action": action(SET_KEY, fix),
    }
    return [
        (Finding(INSERT_BAND_KEY_COLOUR, at(hit[0]), at(hit[-1]), where.scene_id, message, FAIL, data, fix=fix), None)
    ]


def _cropped_message(where: Placed, worst: float, when: str, count: int) -> str:
    return (
        f"insert cropped at t={when}: fit: cover cuts {worst:.0%} of {where.label}'s content to fill its "
        f"quad ({count} frame{'s' if count != 1 else ''}) - a sidebar or a column goes: {_CROPPED_FIX}"
    )


#: The per-frame checks read off every frame's audit (the residual key and the flicker read the
#: same audits in :func:`_compositing_findings`): the audit field, the evidence kind the frame server keeps the worst
#: frame under, whether a higher value is worse, when a value fails, the finding's words and its fix.
_PIXEL_CHECKS: tuple[tuple[str, str, str, bool, Any, Any, str], ...] = (
    (INSERT_CROPPED, "cropped", "cropped", True, lambda v: v > screen_insert.MAX_CONTENT_CUT, _cropped_message, _CROPPED_FIX),
)


def _pixel_findings(where: Placed, frames: dict[int, dict], evidence: dict, at: Any) -> list[tuple[Finding, str | None]]:
    """The findings of :data:`_PIXEL_CHECKS` over the audited *frames* of *where*, each with its worst frame's path."""
    out: list[tuple[Finding, str | None]] = []
    for kind, field, proof_kind, higher_is_worse, fails, message, fix in _PIXEL_CHECKS:
        bad = sorted(n for n, audit in frames.items() if audit.get(field) is not None and fails(float(audit[field])))
        if not bad:
            continue
        proof = _worst(evidence.get(proof_kind) or [])
        pick = max if higher_is_worse else min
        worst_frame = int(proof["frame"]) if proof and int(proof["frame"]) in frames else pick(bad, key=lambda n: frames[n][field])
        worst = float(frames[worst_frame][field])
        out.append(
            (
                Finding(
                    kind,
                    at(bad[0]),
                    at(bad[-1]),
                    where.scene_id,
                    message(where, worst, clock(at(worst_frame)), len(bad)),
                    FAIL,
                    {"insert": where.key, "frames": len(bad), "worst": round(worst, 5), "worst_at": round(at(worst_frame), 3)},
                    fix=fix,
                ),
                proof.get("path") if proof else None,
            )
        )
    return out


def _worst(proofs: list[dict]) -> dict | None:
    return max(proofs, key=lambda proof: float(proof.get("badness") or 0.0)) if proofs else None


#: What a frame's audit says of the insert's colours (``screen_insert.colour_fidelity``).
COLOUR_FIELDS = ("luma_shift", "black_lift", "hue_rotation", "saturation", "cast", "cast_hue", "contrast")


def colour_bad(audit: dict) -> float:
    """How far a frame's colours are past their thresholds (``screen_insert.colour_badness``): past 1 it fails."""
    return screen_insert.colour_badness(audit.get("black_lift"), audit.get("hue_rotation"), audit.get("cast"))


def colour_failures(record: dict) -> list[str]:
    """What is wrong with an insert's colours, as it is drawn: ``["tinted blue (...)", "with its blacks lifted 18%"]``."""
    words = []
    cast = record.get("cast")
    if cast is not None and float(cast) > screen_insert.MAX_CAST:
        named = screen_insert.hue_name(record.get("cast_hue")) or "off grey"
        words.append(f"tinted {named} (a cast of {float(cast):.0f} grey levels on a source with no hue of its own)")
    turned = record.get("hue_rotation")
    if turned is not None and float(turned) > screen_insert.MAX_HUE_ROTATION_DEGREES:
        words.append(f"with its hue turned {float(turned):.0f} degrees")
    lift = record.get("black_lift")
    if lift is not None and float(lift) > screen_insert.MAX_BLACK_LIFT:
        words.append(f"with its blacks lifted {float(lift):.0%}")
    return words


def colour_numbers(record: dict) -> str:
    """Every colour measure of an insert in words: what tunes its ``light``."""
    parts = []
    if record.get("luma_shift") is not None:
        parts.append(f"luminance {float(record['luma_shift']) * 100:+.1f}%")
    if record.get("black_lift") is not None:
        parts.append(f"blacks {float(record['black_lift']) * 100:+.1f}%")
    if record.get("hue_rotation") is not None:
        parts.append(f"hue turned {float(record['hue_rotation']):.1f} degrees")
    if record.get("cast") is not None:
        named = screen_insert.hue_name(record.get("cast_hue"))
        parts.append(f"cast {float(record['cast']):.1f} grey levels" + (f" ({named})" if named and float(record["cast"]) >= 1.0 else ""))
    if record.get("saturation") is not None:
        parts.append(f"saturation {float(record['saturation']):.2f}x")
    if record.get("contrast") is not None:
        parts.append(f"contrast {float(record['contrast']):.2f}x")
    return ", ".join(parts)


def _colour_action(where: Placed, record: dict, still: dict | None = None) -> dict:
    """The one action of an insert's colour finding: its light lowered, for blacks the footage's light
    lifts - the fixtures show the light moves them; else what shows through it - the footage where the matte leaves
    it, or under the source's own translucent pixels. An earlier drawing at another key or light that left the same
    (*still*, :func:`unchanged`) makes it ``not a setting``."""
    coverage = record.get("coverage")
    through = coverage is not None and float(coverage) < 0.98
    if still is not None:
        if through:
            detail = (
                f"the matte leaves the footage showing through {1.0 - float(coverage):.0%} of the quad there - generate "
                "the shot again with the surface one flat key colour"
            )
            return action(REGENERATE_SHOT, _not_a_setting(FOREGROUND, detail, still, where))
        detail = "the source's own translucent pixels drawn over the footage - give the insert a source opaque there"
        return action(REPLACE_SOURCE, _not_a_setting(SOURCE_CONTENT, detail, still, where))
    lift = float(record.get("black_lift") or 0.0)
    light = float(where.light or 0.0)
    if lift > screen_insert.MAX_BLACK_LIFT and light > 0:
        return action(
            LOWER_LIGHT,
            f"lower its light (light: {light:g} now; {INSERT_DEFAULT_LIGHT:g} is the default, 0 draws the source as it is)",
        )
    if through:
        return action(
            REGENERATE_SHOT,
            "the matte leaves the footage showing through it: generate the shot again with the surface one flat key colour",
        )
    return action(REPLACE_SOURCE, "the footage shows through the source's translucent pixels: give the insert a source opaque there")


#: An insert fades in and out over this long (the page's ``insertShown``): a crop is cut once it is whole.
_FADE_SECONDS = INSERT_FADE_SECONDS
#: Two reversals of the matte's coverage or the insert's luma this many frames apart or less are flicker.
FLICKER_SPAN = 6
#: A halo fails on at least this many frames past ``screen_insert.MAX_HALO_SHARE`` (:data:`MIN_FAILING_FRAMES`).
HALO_MIN_FRAMES = MIN_FAILING_FRAMES


def crop_window(where: Placed) -> tuple[float, float]:
    """``(from, to)`` in the video an insert's crops are cut within: where it is whole - its fade in
    done, its fade out not begun - and its scene on screen alone (:attr:`Placed.owned`): never a frame of the
    transition in, nor of the transition out, a blend with the next scene, which would measure the transition and
    not the compositing. An insert never whole there: the middle of its own span. One that runs to its scene's end
    never fades out."""
    first, last = where.start + _FADE_SECONDS, where.end - (0.0 if where.to_end else _FADE_SECONDS)
    if where.owned is not None:
        first, last = max(first, where.owned[0]), min(last, where.owned[1])
    if last <= first:
        middle = (where.start + where.end) / 2
        return middle, middle
    return first, last


def crop_frames(placed: dict[str, Placed], *, fps: float, frame_range: tuple[int, int] | None = None) -> dict[str, set[int]]:
    """The output frames whose insert region is kept as a 100 % crop: each insert's first, middle and last
    frame within its :func:`crop_window` - whole, its scene on screen alone -, within *frame_range* (a scene
    preview's window) when given."""
    rate = max(float(fps), 1.0)
    low, high = frame_range if frame_range is not None else (0, None)
    out: dict[str, set[int]] = {}
    for key, where in placed.items():
        # A frame inside each bound: past the insert's fade in, before its fade out, and in the frames its scene
        # owns alone - from the first at or after its transition in ends to the last before the next one begins.
        first = math.ceil((where.start + _FADE_SECONDS) * rate) + 1
        last = math.floor((where.end - (0.0 if where.to_end else _FADE_SECONDS)) * rate) - 1
        if where.owned is not None:
            first = max(first, math.ceil(where.owned[0] * rate) + 1)
            last = min(last, math.ceil(where.owned[1] * rate) - 2)
        if last < first:
            middle = sum(crop_window(where)) / 2
            first = last = round(middle * rate)
        first = max(first, low)
        last = min(last, high - 1) if high is not None else last
        if last < first:
            continue
        out[key] = {first, (first + last) // 2, last}
    return out


#: The frames of each join between two scenes the render's guard reads once encoded, for each keyed insert drawn on it
#: (:func:`guard_frames`) ...
GUARD_FRAMES = 4
#: ... and how much of an insert a frame shows for the encoded frame itself to be read (:func:`judge_dropped`): its scene
#: alone there - a cut's frames, a transition's ends. Its scene's frame drawn alone is read on every one.
GUARD_ALONE = 0.98


def _spread(frames: list[int], count: int) -> list[int]:
    """*count* of *frames* (in order) spread over them, the first and the last included; all of them when fewer."""
    if len(frames) <= count:
        return list(frames)
    return sorted({frames[round(index * (len(frames) - 1) / (count - 1))] for index in range(count)})


def guard_frames(placed: dict[str, Placed], *, frame_range: tuple[int, int] | None = None) -> dict[int, list[str]]:
    """``output frame -> the keyed inserts drawn on it`` that the render's guard reads on its encoded frames
    (:func:`dropped`): :data:`GUARD_FRAMES` frames spread over each join of an insert's scene with another, among those
    its layer is drawn on (:attr:`Placed.expected`) - picked over the whole join, so a scene range or a preview
    drawing part of it picks the same frames -, those within *frame_range* when given."""
    low, high = frame_range if frame_range is not None else (0, None)
    out: dict[int, list[str]] = {}
    for key, where in placed.items():
        if not where.keyed or not where.expected:
            continue
        drawn = set(where.expected_frames())
        for first, end in where.joins:
            for frame in _spread([n for n in range(first, end) if n in drawn], GUARD_FRAMES):
                if frame >= low and (high is None or frame < high):
                    out.setdefault(frame, []).append(key)
    return out


def _quad_on_frame(box: Any, payload: dict, audit: dict, size: tuple[int, int]) -> list[tuple[float, float]] | None:
    """An insert's quad in pixels of an output frame of *size*: its quad on the footage - the bezel found on that
    frame (*audit*'s ``refined``), else the one given - carried from the box the page places its layer on (the
    payload's ``box``, else its quad's, 0-1 of the footage, clamped as ``insertBox`` clamps it) to where the page placed
    that box on the frame (*box*, 0-1 of the frame: ``compositor.FrameServer.place``)."""
    corners = payload.get("box") or payload.get("quad")
    quad = audit.get("refined") or payload.get("quad")
    if not corners or not quad:
        return None
    xs = [min(max(float(point[0]), 0.0), 1.0) for point in corners]
    ys = [min(max(float(point[1]), 0.0), 1.0) for point in corners]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    if right - left <= 1e-6 or bottom - top <= 1e-6:
        return None
    x0, y0, x1, y1 = (float(value) for value in box)
    width, height = size
    return [
        ((x0 + (float(x) - left) / (right - left) * (x1 - x0)) * width, (y0 + (float(y) - top) / (bottom - top) * (y1 - y0)) * height)
        for x, y in quad
    ]


async def dropped(
    video_path: str,
    audits: dict[str, dict],
    placed: dict[str, Placed],
    guard: dict[int, list[str]],
    document: dict,
    keys: dict[str, Any],
    *,
    fps: float,
    workdir: str | Any,
    first_frame: int = 0,
    offset: float = 0.0,
) -> list[tuple[Finding, str | None]]:
    """The ``insert_dropped`` findings of a render's encoded *video_path*, each with the frame that shows it:
    each frame of *guard* (:func:`guard_frames`) read back off the video and judged (:func:`judge_dropped`).
    *first_frame* is the video's first output frame and *offset* its start in the plan (a scene preview's window)."""
    import asyncio
    from pathlib import Path

    from app.services.video_studio import encoder

    rate = max(float(fps), 1.0)
    wanted = sorted(frame for frame in guard if frame >= first_frame)
    if not wanted:
        return []
    out_dir = Path(workdir) / "guard"
    out_dir.mkdir(parents=True, exist_ok=True)
    read = await encoder.extract_frames(video_path, [frame - first_frame for frame in wanted], fps=rate, out_dir=str(out_dir))
    encoded = {frame: path for frame in wanted if (path := read.get(frame - first_frame)) is not None}
    return await asyncio.to_thread(judge_dropped, encoded, audits, placed, guard, document, keys, fps=rate, offset=offset)


def key_where_text(where: dict[str, float] | None) -> str:
    """Where the key's colour sat on a frame read missing or dropped by its key (``screen_insert.KeyRead``): the
    quad's inside, which judged it, then its edge strip and the bezel margin, which did not, and the source's own key
    hue allowed - ``""`` when not measured."""
    if not where:
        return ""
    parts = [f"{float(where.get('inside') or 0.0) * 100:.2f}% inside the quad"]
    for name, words in (("edge", "on its edge strip along the bezel"), ("margin", "in the bezel margin past it")):
        if float(where.get(name) or 0.0) > 0.0:
            parts.append(f"{float(where[name]) * 100:.2f}% {words}, not counted")
    if float(where.get("source_key_hue") or 0.0) > 0.0:
        parts.append(f"{float(where['source_key_hue']) * 100:.2f}% the source's own key hue, allowed")
    return f" ({'; '.join(parts)})"


def judge_dropped(
    encoded: dict[int, Any],
    audits: dict[str, dict],
    placed: dict[str, Placed],
    guard: dict[int, list[str]],
    document: dict,
    keys: dict[str, Any],
    *,
    fps: float,
    offset: float = 0.0,
) -> list[tuple[Finding, str | None]]:
    """The ``insert_dropped`` findings of a render's guard, each with the frame that shows it: *encoded* is
    ``output frame -> its encoded picture`` (an ``H x W x 3`` array, or the path of its PNG - then the evidence a
    finding names).

    On each frame of *guard*, for each keyed insert drawn on it, the key's
    colour is measured inside its quad (:func:`_quad_on_frame`): the residual
    key's measure (``screen_insert.key_left``) less the source's own key hue on
    that frame. It is read on its scene's frame drawn alone (``alone`` in
    *audits*: the page's ``alone()``) - its own finished frame, never blended
    with the scene beside it, where a key-hued picture of that scene would read
    as the key - and, where the frame shows its scene alone (``placed``: at
    least :data:`GUARD_ALONE` of the insert shown), on the encoded frame too.
    Past ``screen_insert.RESIDUAL_FAIL_SHARE`` - or a layer the page placed
    nowhere - the insert was dropped there: a failure whatever the frame
    server's audits said. *keys* are each insert's key (``KeySpec.key()``).

    A frame the guard could not read - neither its scene drawn alone
    nor where the page placed the insert kept for it (a segment an attempt
    before drew), or no picture left to read - is no measure of an absence: a
    warning that says the frames it was asked for and those it read, never a
    failure.
    """
    import numpy as np
    from PIL import Image

    from app.services.video_studio import screen_insert

    rate = max(float(fps), 1.0)
    payloads = {
        str(payload.get("src")): payload
        for scene in document.get("scenes") or []
        for payload in scene.get("inserts") or []
        if isinstance(payload, dict)
    }
    pictures: dict[str, Any] = {}

    def picture_of(held: Any) -> Any:
        """The picture of a reading, or ``None`` for a file no longer there (an attempt before kept its audits alone)."""
        if not isinstance(held, str):
            return np.asarray(held)
        if held not in pictures:
            try:
                pictures[held] = np.asarray(Image.open(held).convert("RGB"))
            except OSError:
                pictures[held] = None
        return pictures[held]

    #: ``key -> [(frame, share of the quad, the frame's path, where it was read, where the key's colour sat)]``: ``None``
    #: placed nowhere.
    hits: dict[str, list[tuple[int, float | None, str | None, str, dict[str, float] | None]]] = {}
    #: ``key -> the guard frames asked of it``, and those it could not read at all.
    asked: dict[str, int] = {}
    unread: dict[str, list[int]] = {}
    for frame in sorted(guard):
        for key in guard[frame]:
            if key not in keys or key not in placed:
                continue
            asked[key] = asked.get(key, 0) + 1
            part = audits.get(key) or {}
            audit = {int(n): value for n, value in (part.get("frames") or {}).items()}.get(frame) or {}
            allowed = float(audit.get("source_key_hue") or 0.0) + screen_insert.RESIDUAL_FAIL_SHARE
            readings = []
            alone = {int(n): value for n, value in (part.get("alone") or {}).items()}.get(frame)
            if alone is not None:
                readings.append((alone.get("path") or alone.get("file"), alone.get("box"), "on its scene drawn alone"))
            shown = {int(n): value for n, value in (part.get("placed") or {}).items()}.get(frame)
            if shown is not None and len(shown) > 4 and float(shown[4]) >= GUARD_ALONE and frame in encoded:
                readings.append((encoded[frame], shown[:4], "on the encoded frame"))
            read = False
            for held, box, read_on in readings:
                path = held if isinstance(held, str) else None
                if box is None:
                    # Its scene drawn alone with no layer of it: the page placed it nowhere, a measured absence.
                    hits.setdefault(key, []).append((frame, None, path, read_on, None))
                    read = True
                    continue
                picture = picture_of(held) if held is not None else None
                if picture is None:
                    continue
                corners = _quad_on_frame(box, payloads.get(key) or {}, audit, (picture.shape[1], picture.shape[0]))
                if corners is None:
                    continue
                read = True
                # Inside the quad past its edge strip: the key's colour along the bezel and past it is the
                # footage's, as the residual key judges it on the layer - never the insert dropped.
                keyed = screen_insert.key_read(picture, corners, keys[key])
                if keyed.inside > allowed:
                    own = round(allowed - screen_insert.RESIDUAL_FAIL_SHARE, 5)
                    hits.setdefault(key, []).append((frame, keyed.inside, path, read_on, {**keyed.as_dict(), "source_key_hue": own}))
            if not read:
                unread.setdefault(key, []).append(frame)
    out: list[tuple[Finding, str | None]] = []
    for key, frames in unread.items():
        where = placed[key]
        count = asked[key]
        first = frames[0]
        others = f" and {len(frames) - 1} more" if len(frames) > 1 else ""
        fix = "nothing to change in the video: what the guard read is judged; render again for a guard that reads all of them"
        out.append(
            (
                Finding(
                    INSERT_DROPPED,
                    max(0.0, first / rate - offset),
                    max(0.0, frames[-1] / rate - offset),
                    where.scene_id,
                    f"insert guard could not read frame {first}{others} of {where.label} on its scene's join with the "
                    f"scene beside it: neither its scene drawn alone nor where the page placed it was kept there - "
                    f"requested {count} guard frame{'s' if count != 1 else ''}, read {count - len(frames)} - {fix}",
                    WARN,
                    {
                        "insert": key,
                        "frame": first,
                        "frames": len(frames),
                        "requested": count,
                        "read": count - len(frames),
                        "unread": True,
                        "gate": "dropped",
                        "action": action(NOTHING, fix),
                    },
                    fix=fix,
                ),
                None,
            )
        )
    unexplained = "nothing in the video causes it: render again, and report the render with this frame if it stays"
    for key, found in hits.items():
        where = placed[key]
        frame, share, path, read_on, sat = max(found, key=lambda hit: (hit[1] is None, hit[1] or 0.0))
        found = sorted(found, key=lambda hit: hit[0])
        # Frames all inside one transition are kept off by a spec change; others are a fact.
        chosen = join_action(document, where, [hit[0] for hit in found], fps=rate) or action(NOTHING, unexplained)
        fix = chosen["text"]
        shown = (
            f"shows the key's colour on {share * 100:.2f}% of its quad {read_on}{key_where_text(sat)}"
            if share is not None
            else f"has no layer where the page should have placed it {read_on}"
        )
        out.append(
            (
                Finding(
                    INSERT_DROPPED,
                    max(0.0, found[0][0] / rate - offset),
                    max(0.0, found[-1][0] / rate - offset),
                    where.scene_id,
                    f"insert dropped at t={clock(max(0.0, frame / rate - offset))}: {where.label} {shown} on frame {frame}, "
                    f"on its scene's join with the scene beside it ({len(found)} reading{'s' if len(found) != 1 else ''} "
                    f"there past {screen_insert.RESIDUAL_FAIL_SHARE * 100:.1f}%) - {fix}",
                    FAIL,
                    {
                        "insert": key,
                        "frame": frame,
                        "frames": len(found),
                        **({"share": round(share, 5)} if share is not None else {"placed": False}),
                        **({"key_where": sat} if sat else {}),
                        "transition": True,
                        "span": _plan_span(found[0][0], found[-1][0], rate),
                        "gate": "dropped",
                        "threshold": screen_insert.RESIDUAL_FAIL_SHARE,
                        "action": chosen,
                    },
                    fix=fix,
                ),
                path,
            )
        )
    return out


#: Under this variance of its high-passed luma (grey levels squared) a quad shows one flat colour (the presence
#: gate): the bezel's colour, never a source - judged so only where the source expected there is not flat itself ...
FLAT_VAR_MIN = 6.0
#: ... and under this structural similarity to the source frame expected there, the source is not on its quad.
PRESENCE_SSIM_MIN = 0.3
#: An encoded frame is read for the source where at least this much of its insert shows (the page's ``insertRects``)
#: and, on a blended frame read unblended, where its alpha there is at least this: its scene alone, or the side
#: of a transition it still holds. Under it the frame is faint, a fact of its report and never a finding - the scene
#: beside it outweighs what is left of the insert (the bright text of a statement crossing its quad at alpha 0.55).
PRESENCE_MIN_SHOWN = 0.65
#: Under this share shown, a frame blends the insert with what the frame shows beside it - a transition's frame:
#: it is read unblended, with its scene painted black and white on that frame (``compositor.FrameServer.keep_blend``) -
#: what the frame shows of everything else, and how much of the scene it shows at each pixel.
BLEND_WHOLE = 0.995
#: A blended frame whose scene was not painted black and white for it (a segment drawn by an older render) is read raw only
#: where it shows this much of the insert: past it, what blends with it would be judged as the insert's loss.
BLEND_RAW_SHOWN = 0.9
#: What stands in front covering more than this share of the quad leaves the source unjudged on that frame.
PRESENCE_MAX_FOREGROUND = 0.5
#: The quad is read on a grid at most this long, its edges (the bezel's lock, the glow) left out by this share a side.
PRESENCE_SIDE = 192
PRESENCE_INSET = 0.06
#: What its scene draws over an insert - its text, chips and overlays, the subtitles, the watermark (the page's
#: ``insertRects``, the audits' ``over``) - is left out of the quad read, grown by this many grid pixels (a glyph's
#: shadow, its blur coming in, the codec's ringing, the window the structural similarity reads); a frame where it
#: leaves less than this share of the quad uncovered, as the page gives its boxes, is not judged.
PRESENCE_COVER_GROW = 2
PRESENCE_MIN_UNCOVERED = 0.4
#: An ``insert flickers`` on this many frames or fewer names each of them (``data.missed``); past it, its first and how
#: many.
MAX_NAMED_FRAMES = 5
#: The encoded video and a video source are decoded at most this wide for the gate.
PRESENCE_WIDTH = 960
PRESENCE_SOURCE_SIDE = 480
#: Inside a transition or a fade (:attr:`Placed.blends`) a quad under :data:`FLAT_VAR_MIN` is judged against the frame
#: around it: that frame read in this grid of blocks (:func:`frame_blocks`), the quad grown by this share of its size
#: left out of it (its bezel, its glow), a block where at least half of it lies outside ...
PRESENCE_BLOCKS = (16, 9)
PRESENCE_AROUND_MARGIN = 0.15
#: ... compared with the same block of its scene's un-transitioned frame - one of this many each side of the window
#: (:func:`frame_attenuation`) - where that block holds this much detail, on at least this many blocks ...
PRESENCE_REFERENCE_FRAMES = 3
PRESENCE_TEXTURE_MIN = 1.0
PRESENCE_MIN_BLOCKS = 4
#: ... and the quad present when its detail is at least this share of its un-transitioned frame's, attenuated as the
#: frame around it is: a source dims and blurs with its scene, a flat surface has no detail to keep.
PRESENCE_TRACK_SHARE = 0.2


def presence_frames(placed: dict[str, Placed], *, frame_range: tuple[int, int] | None = None) -> dict[int, list[str]]:
    """``output frame -> the inserts`` the presence gate reads on the encoded frames (:func:`presence`): every
    frame each insert's scene paints its layer on (:attr:`Placed.expected`) and every frame it shows whole on
    (:attr:`Placed.whole`), within *frame_range* when given. Where the page places each insert on them is kept
    (``compositor.FrameServer.place``)."""
    out: dict[int, list[str]] = {}
    for key, where in placed.items():
        for frame in sorted(set(where.expected_frames(frame_range)) | set(where.whole_frames(frame_range))):
            out.setdefault(frame, []).append(key)
    return out


def _luma(rgb: Any) -> Any:
    import numpy as np

    return np.asarray(rgb, dtype=np.float32)[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def detail_variance(luma: Any, mask: Any) -> float:
    """The variance of *luma*'s detail inside *mask*: what a Gaussian of 2 px leaves of it - a light's slow
    gradient over a flat surface is none of it, a line of text is."""
    import numpy as np
    from scipy import ndimage

    if not mask.any():
        return 0.0
    detail = luma - ndimage.gaussian_filter(luma, 2.0)
    return float(np.var(detail[mask]))


def frame_blocks(picture: Any, corners: list[tuple[float, float]]) -> Any:
    """The detail of the encoded *picture* around an insert's quad (*corners*, in its pixels): what :func:`detail_variance`
    reads, in each block of a :data:`PRESENCE_BLOCKS` grid over the frame, the quad grown by
    :data:`PRESENCE_AROUND_MARGIN` left out - NaN for a block mostly inside it. A caption or a mark over a few blocks
    moves none of their median."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage

    luma = _luma(picture)
    detail = luma - ndimage.gaussian_filter(luma, 2.0)
    energy = detail * detail
    height, width = luma.shape
    cx, cy = sum(x for x, _ in corners) / len(corners), sum(y for _, y in corners) / len(corners)
    grow = 1.0 + PRESENCE_AROUND_MARGIN
    cover = Image.new("L", (width, height), 0)
    ImageDraw.Draw(cover).polygon([(cx + (x - cx) * grow, cy + (y - cy) * grow) for x, y in corners], fill=1)
    outside = np.asarray(cover) == 0
    columns, rows = PRESENCE_BLOCKS
    xs = np.linspace(0, width, columns + 1).round().astype(int)
    ys = np.linspace(0, height, rows + 1).round().astype(int)
    out = np.full((rows, columns), np.nan, dtype=np.float32)
    for row in range(rows):
        for column in range(columns):
            keep = outside[ys[row] : ys[row + 1], xs[column] : xs[column + 1]]
            if keep.size and keep.mean() >= 0.5:
                out[row, column] = float(energy[ys[row] : ys[row + 1], xs[column] : xs[column + 1]][keep].mean())
    return out


def own_blocks(picture: Any, toned: tuple[Any, Any], corners: list[tuple[float, float]]) -> Any:
    """:func:`frame_blocks` of the scene a blended *picture* shows an insert's quad in: the frame less the scene
    painted black, over how much of it the frame shows (*toned*: its black and its white paintings) - what the
    transition left of that scene's own detail, never the scene coming in over it. A block showing under
    :data:`PRESENCE_MIN_SHOWN` of the scene is NaN."""
    import numpy as np

    black, white = toned
    weight = np.clip((white - black).mean(axis=2) / 255.0, 0.0, 1.0)
    own = np.clip((np.asarray(picture, dtype=np.float32)[..., :3] - black) / np.maximum(weight, 1.0 / 255.0)[..., None], 0.0, 255.0)
    blocks = frame_blocks(own, corners)
    columns, rows = PRESENCE_BLOCKS
    xs = np.linspace(0, weight.shape[1], columns + 1).round().astype(int)
    ys = np.linspace(0, weight.shape[0], rows + 1).round().astype(int)
    for row in range(rows):
        for column in range(columns):
            if float(weight[ys[row] : ys[row + 1], xs[column] : xs[column + 1]].mean()) < PRESENCE_MIN_SHOWN:
                blocks[row, column] = np.nan
    return blocks


def video_fade_left(document: dict, at: float) -> float:
    """How much of the whole frame at *at* seconds the video's fades from and to black leave (the page's ``#fade``:
    its opacity the larger of the fade in's and the fade out's), 1 outside them."""
    duration = float(document.get("duration") or 0.0)
    fade_in, fade_out = float(document.get("fade_in") or 0.0), float(document.get("fade_out") or 0.0)
    coming = 1.0 - min(max(at / fade_in, 0.0), 1.0) if fade_in > 0 else 0.0
    going = min(max((at - (duration - fade_out)) / fade_out, 0.0), 1.0) if fade_out > 0 else 0.0
    return 1.0 - max(coming, going)


def frame_attenuation(blocks: Any, reference: Any) -> float | None:
    """How much a transition left of the detail around an insert's quad: a frame's *blocks* against its scene's
    un-transitioned frame's *reference* (:func:`frame_blocks`), the median ratio over the blocks holding
    :data:`PRESENCE_TEXTURE_MIN` there; ``None`` on fewer than :data:`PRESENCE_MIN_BLOCKS` of them."""
    import numpy as np

    both = np.isfinite(blocks) & np.isfinite(reference) & (reference >= PRESENCE_TEXTURE_MIN)
    if int(both.sum()) < PRESENCE_MIN_BLOCKS:
        return None
    return float(np.median(blocks[both] / reference[both]))


def around_detail(blocks: Any) -> float:
    """The detail of the frame around an insert's quad (:func:`frame_blocks`): its blocks' median, 0 with none."""
    import numpy as np

    finite = blocks[np.isfinite(blocks)]
    return float(np.median(finite)) if finite.size else 0.0


def structural_similarity(seen: Any, expected: Any, mask: Any) -> float | None:
    """The mean SSIM (Gaussian window, sigma 1.5) inside *mask* of two lumas, each brought to the same mean and spread
    over it first: a fade, a transition's dimming or the footage's light laid over an insert leave its
    structure, which is what is compared. ``None`` when *expected* is flat there (nothing to recognise it by); 0 for a
    flat *seen*."""
    import numpy as np
    from scipy import ndimage

    if int(mask.sum()) < 16:
        return None
    a, b = np.asarray(seen, dtype=np.float32), np.asarray(expected, dtype=np.float32)
    spread_b = float(b[mask].std())
    if spread_b < 1.0:
        return None
    spread_a = float(a[mask].std())
    if spread_a < 1.0:
        return 0.0
    a = 128.0 + 40.0 * (a - float(a[mask].mean())) / spread_a
    b = 128.0 + 40.0 * (b - float(b[mask].mean())) / spread_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = ndimage.gaussian_filter(a, 1.5), ndimage.gaussian_filter(b, 1.5)
    var_a = ndimage.gaussian_filter(a * a, 1.5) - mu_a * mu_a
    var_b = ndimage.gaussian_filter(b * b, 1.5) - mu_b * mu_b
    cov = ndimage.gaussian_filter(a * b, 1.5) - mu_a * mu_b
    ssim = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2))
    return float(ssim[mask].mean())


def _hex(rgb: Any) -> str:
    return "#" + "".join(f"{min(max(round(float(v)), 0), 255):02X}" for v in list(rgb)[:3])


@dataclass
class Presence:
    """What the presence gate read off the encoded frames (:func:`presence`): its findings, each with the frame
    that shows it, and per insert ``{frames, missing, unjudged, joins, joins_missing, first_missing?, blended?,
    least_alpha?, blended_unread?, faint?, covered?, undrawn?, dimmed?, join_spans?, disagrees?}`` - what its report
    says (:func:`reports`): the transition's frames read unblended, the least alpha the insert had on one, those
    left unread, and those showing too little to judge, the frames what its scene draws over it left too little
    of it uncovered on to judge, and those of its missing ones its layer was not drawn on (``insert flickers``).
    ``dimmed`` is ``[{window, frames, whole_frame, recognised}]``: the frames of each transition or fade its
    quad was under the floor on and judged present, how many of them had the whole frame under the floor too, and how
    many its source's structure was recognised on before any frame around it was read; ``recognised`` the same
    outside any window. ``join_spans`` is ``{present, missing}``, the plan's spans of its
    join frames read each way, and ``disagrees`` the earlier drawing of the same join that read them the other way
    (:func:`agree_on_joins`)."""

    findings: list[tuple[Finding, str | None]] = dataclass_field(default_factory=list)
    by_key: dict[str, dict] = dataclass_field(default_factory=dict)


@dataclass
class _Dimmed:
    """A frame inside a transition or a fade whose quad read under :data:`FLAT_VAR_MIN`, judged once its scene's
    un-transitioned frame beside the window is read (:meth:`PresenceGate._settle`)."""

    frame: int
    window: tuple[int, int, str]
    #: Its quad's detail and the blocks around it, both of its own scene: read unblended where the frame blends it.
    detail: float
    colour: str
    blocks: Any
    picture: Any
    #: The insert's alpha on a blended frame read unblended.
    alpha: float | None = None


#: An insert's source is looked for around the quad the gate reads (:meth:`PresenceGate._looked_around`) on this
#: many of its first frames read whole ...
MEASURE_FRAMES = 3
#: ... its quad moved up to this share of its own size each way, in steps of this share, then a third of a step around the
#: best ...
MEASURE_SEARCH = 0.36
MEASURE_STEP = 0.06
#: ... and found there - its source itself, at this structural similarity at least, never an edge of what surrounds a flat
#: quad - this much more like it than where it is read, on each of them at one place within a step: the gate reads
#: another region than the one the page drew the insert on - its measurement lost, never the insert missing nor present
#: there. A read already this close to its source is never searched.
MEASURE_FOUND_SSIM = 0.6
MEASURE_GAIN = 0.1


@dataclass
class _Read:
    """One of an insert's first frames read whole: what :meth:`PresenceGate._looked_around` reads again."""

    frame: int
    picture: Any
    corners: list[tuple[float, float]]
    grid: tuple[int, int]
    #: ``(luma, mask)`` of each source frame it was compared with, the mask inset.
    expected: list[tuple[Any, Any]]
    #: Where the page placed it there, ``[x0, y0, x1, y1]`` 0-1 of the frame.
    box: list[float]


def _moved_words(moved: list[float]) -> str:
    """``8% of the quad's width right and 3% of its height down of it``: where around the quad read its source is."""
    dx, dy = (float(value) for value in moved)
    parts = [
        *([f"{abs(dx):.0%} of the quad's width {'right' if dx > 0 else 'left'}"] if abs(dx) >= 0.005 else []),
        *([f"{abs(dy):.0%} of its height {'down' if dy > 0 else 'up'}"] if abs(dy) >= 0.005 else []),
    ]
    return " and ".join(parts) + " of it"


def lost_words(lost: dict) -> str:
    """What an insert's lost measurement says (:class:`PresenceGate`): where it was read - the encoded frame,
    the box, the size it was decoded at - and why that is not where it is drawn: its source found moved off it, or the
    bezel its save or a drawing before found, at the size each read it, found by no frame of this render."""
    box = ", ".join(f"{float(value):.3f}" for value in lost.get("box") or [])
    size = lost.get("read_at")
    read = f"encoded frame {int(lost['frame'])} at box [{box}] (0-1 of the frame{f', decoded at {size[0]}x{size[1]}' if size else ''})"
    if lost.get("why") == "moved":
        return (
            f"its source is not under the box read on {read}: it is found {_moved_words(lost.get('moved') or [0.0, 0.0])} "
            f"(SSIM {float(lost.get('ssim') or 0.0):.2f}) - the gate read another region than the one the page drew it on"
        )
    found = "; ".join(
        StoredBezel(float(bezel["alignment"]), (int(bezel["measured_at"][0]), int(bezel["measured_at"][1])), str(bezel["by"]), bezel.get("where")).words()
        for bezel in lost.get("earlier") or []
    )
    drawn, audited = lost.get("drawn_at"), int(lost.get("audited") or 0)
    rendered = (
        f"no bezel on any of the {audited} frame(s) it audited of it at {drawn[0]}x{drawn[1]}"
        if audited and drawn
        else "no frame of it audited"
    )
    return f"its bezel found ({found}), where this render read {rendered} - read on {read}"


def _unrecord(first: list[_Read], frame: int) -> None:
    """*frame* left out of an insert's first frames read whole: it was not judged after all."""
    if first and first[-1].frame == frame:
        first.pop()


class PresenceGate:
    """The presence gate over a render's or a preview's own encoded frames, one frame at a time (:meth:`read`).

    For every insert, on every frame of :func:`presence_frames`, inside its quad as the page placed it
    (``placed`` in *audits*, :func:`_quad_on_frame`): a keyed one's key colour where its scene shows alone
    (``screen_insert.key_left``); and where it shows whole and at least :data:`PRESENCE_MIN_SHOWN` of it is on screen, a
    flat fill (:func:`detail_variance` under :data:`FLAT_VAR_MIN` where its source is not flat) and its source's
    presence (:func:`structural_similarity` over :data:`PRESENCE_SSIM_MIN` against the source frame expected there, the
    one before or after it). A frame it shows whole on that the page placed it nowhere is missing too. Any of them on
    one frame is ``insert missing``: never the pre-composite layer, the frame the audience sees. It reads only the
    frames that are its scene's (:meth:`Placed.owns_frame`), those its flicker is judged on: a frame of a
    transition that is not - the first of its transition in, the scene before whole there - is faint, a fact.

    What its scene draws over the quad (a statement's lines and chips, an overlay, the subtitles - the audits'
    ``over``, :meth:`_covered`) is left out of it before the two are read: on the rest of the quad, where that is at
    least :data:`PRESENCE_MIN_UNCOVERED` of it - under it the frame is not judged (``covered``), its insert out of
    sight. A frame shown whole, never in a transition, that it reads the insert missing on and the frame server drew
    no layer of (:func:`missing_frames`, the page log) is ``insert flickers`` instead: its layer not drawn there. A
    frame the page log misses and the encoded frame shows drawn is no finding.

    A frame that shows less than :data:`BLEND_WHOLE` of it blends it with what the frame shows beside it - a
    crossfade's incoming scene - and is read unblended (:meth:`_unblended`): the frame less its scene painted
    black, over how much of the scene the frame shows (the white painting less the black one), judged where that is
    at least :data:`PRESENCE_MIN_SHOWN`. A finding there names the insert's alpha on that frame.

    A quad under the floor whose source's structure is still read on it (:func:`structural_similarity` over
    :data:`PRESENCE_SSIM_MIN`) is present: a dark UI of dim text reads under the floor once a crossfade or the
    encoder dims it, and the frame around it - which a crossfade dims less than the quad it fades in - says nothing
    about it. The flat test judges a quad the source is not recognised on.

    Inside a transition or a fade (:attr:`Placed.blends`) the whole scene is dimmed or blurred, its insert with
    it - unblending takes the scene beside it away, never the blur its own layer is drawn with: a quad under the floor
    there is judged against the frame around it, its own scene's (:func:`own_blocks`: unblended too, where the
    frame blends it - a statement's bright text coming in over a black quad is no detail of its scene). Against its
    scene's un-transitioned frame beside the window
    (:data:`PRESENCE_REFERENCE_FRAMES`), it is present when its detail kept :data:`PRESENCE_TRACK_SHARE` of that frame's
    quad's, attenuated as the frame around it was (:func:`frame_attenuation`); with no such frame to compare, when the
    whole frame around it is under the floor too (:func:`around_detail`) - the screen alone flat is the insert missing.
    Either is a fact of its report.

    The gate checks its own measurement (``insert_measurement_lost``): an insert *unmeasured* - drawn whole by
    a render whose frames found none of the bezel its save or a drawing before it found (:func:`unmeasured`) - is not
    read, and one whose source is clearly more like it at one place around the quad read than where it is read, on
    every one of its first frames read whole (:meth:`_looked_around`), was read on another region than the one the page
    drew it on. Either is its measurement lost, named with the frame, the box and the size each was read at - never its
    source missing from N of N frames, nor present on a region it is not drawn on.
    """

    def __init__(
        self,
        audits: dict[str, dict],
        placed: dict[str, Placed],
        document: dict,
        inserts: dict[str, Any],
        *,
        fps: float,
        offset: float = 0.0,
        frame_range: tuple[int, int] | None = None,
        evidence_dir: Any = None,
        unmeasured: dict[str, dict] | None = None,
    ) -> None:
        self.audits, self.placed, self.inserts, self.document = audits, placed, inserts, document
        self.rate = max(float(fps), 1.0)
        self.offset = offset
        self.evidence_dir = evidence_dir
        self.frames = presence_frames(placed, frame_range=frame_range)
        self.payloads: dict[str, dict] = {}
        self.starts: dict[str, float] = {}
        for scene in document.get("scenes") or []:
            for payload in scene.get("inserts") or []:
                if isinstance(payload, dict) and payload.get("src"):
                    self.payloads[str(payload["src"])] = payload
                    self.starts[str(payload["src"])] = float(scene.get("start") or 0.0)
        self.whole = {key: set(where.whole_frames(frame_range)) for key, where in placed.items()}
        self._placed = {key: placements_of(audits.get(key) or {}) for key in placed}
        self._audits = {key: {int(n): a for n, a in ((audits.get(key) or {}).get("frames") or {}).items()} for key in placed}
        #: ``key -> frame -> {"black": path, "white": path}``: its scene painted black and white on a frame blending it.
        self._blends = {key: {int(n): dict(toned) for n, toned in ((audits.get(key) or {}).get("blend") or {}).items()} for key in placed}
        #: ``key -> frame -> [[x0, y0, x1, y1], ...]``, 0-1 of the frame: what its scene draws over it there.
        self._over = {key: {int(n): list(boxes) for n, boxes in ((audits.get(key) or {}).get("over") or {}).items()} for key in placed}
        #: ``key -> the frames`` the page draws its layer on that the frame server drew none of (the page log):
        #: those read missing off the encoded frame, shown whole, are its ``insert flickers``.
        self.undrawn = {
            key: set(missing_frames(where, self._audits[key], frame_range, self._placed[key])) for key, where in placed.items()
        }
        #: ``key -> how many`` frames left too little of its quad uncovered by what its scene draws over it to judge.
        self.covered: dict[str, int] = {}
        self.read_frames: dict[str, list[int]] = {}
        self.unjudged: dict[str, int] = {}
        #: ``key -> frame -> the insert's alpha there`` of the blended frames read unblended, and ``key -> how
        #: many`` blended frames were left unread: no black and white painting kept for them, and too blended to read raw.
        self.alphas: dict[str, dict[int, float]] = {}
        self.blended_unread: dict[str, int] = {}
        #: ``key -> how many`` frames it shows whole on that show less than :data:`PRESENCE_MIN_SHOWN` of it, as placed or
        #: unblended: faint, not judged.
        self.faint: dict[str, int] = {}
        #: ``key -> how many`` frames it shows whole on that the video's fade from or to black leaves less than
        #: :data:`PRESENCE_MIN_SHOWN` of (:func:`video_fade_left`): not judged.
        self.faded: dict[str, int] = {}
        #: ``key -> [(frame, why, value, colour, path, alpha)]``: *alpha* the insert's on a blended frame read unblended.
        self.missing: dict[str, list[tuple[int, str, float | None, str | None, str | None, float | None]]] = {}
        #: ``key -> frame -> where the key's colour sat`` on a frame read missing by its key: ``{inside, edge,
        #: margin, source_key_hue}``, each a share of the quad (``screen_insert.KeyRead``).
        self.key_where: dict[str, dict[int, dict[str, float]]] = {}
        #: The un-transitioned frames each side of an insert's windows, and what they read once judged present:
        #: ``frame -> (its quad's detail, the blocks around it)``.
        self.references: dict[str, set[int]] = {}
        for key, where in placed.items():
            near = {
                frame
                for first, end, _what in where.blends
                for frame in (*range(first - PRESENCE_REFERENCE_FRAMES, first), *range(end, end + PRESENCE_REFERENCE_FRAMES))
            }
            self.references[key] = {frame for frame in near & self.whole[key] if where.blend_at(frame) is None}
        self.clean: dict[str, dict[int, tuple[float, Any]]] = {}
        self.pending: dict[str, list[_Dimmed]] = {}
        #: ``key -> {window -> [frames judged present, of them with the whole frame under the floor]}``.
        self.dimmed: dict[str, dict[str, list[int]]] = {}
        #: ``key -> how many`` frames outside any window read under the floor whose source's structure was recognised
        #: there: present, said as a fact.
        self.recognised: dict[str, int] = {}
        #: Each insert's first frames read whole (:data:`MEASURE_FRAMES`), and the inserts whose measurement is
        #: lost: ``key -> {why, frame, box, read_at, ...}`` (``why``: ``unmeasured``, given, or ``moved``,
        #: :meth:`_looked_around`) - none of their readings said, all of them of another region.
        self.first: dict[str, list[_Read]] = {}
        self.lost: dict[str, dict] = {key: {"why": "unmeasured", **record} for key, record in (unmeasured or {}).items() if key in placed}
        #: The size the encoded frames are read at, ``(w, h)``: the decoded frame's own.
        self.size: tuple[int, int] | None = None

    def _looked_around(self, read: _Read) -> tuple[float, float, float] | None:
        """``(dx, dy, ssim)``: where around the quad read on *read*'s frame its source is, the quad moved by *dx* and
        *dy* of its own width and height (:data:`MEASURE_SEARCH`, :data:`MEASURE_STEP`), when it is
        :data:`MEASURE_GAIN` more like it there than where it is read - ``None`` when where it is read is as close as
        anywhere near: the measurement holds, the insert present or missing there."""
        import numpy as np

        corners = np.asarray(read.corners, dtype=float)
        quad_w, quad_h = quad_size(tuple(tuple(point) for point in corners))
        pixels = np.ascontiguousarray(read.picture[..., :3])

        def score(dx: float, dy: float) -> float:
            moved = [tuple(point) for point in corners + np.array([dx * quad_w, dy * quad_h])]
            seen = _luma(screen_insert.rectified(pixels, moved, read.grid, rim=0.0))
            found = [s for luma, mask in read.expected if (s := structural_similarity(seen, luma, mask)) is not None]
            return max(found) if found else -1.0

        here = score(0.0, 0.0)
        if here >= 1.0 - MEASURE_GAIN:
            return None
        coarse = np.arange(-MEASURE_SEARCH, MEASURE_SEARCH + 1e-9, MEASURE_STEP)
        best = max(((score(dx, dy), dx, dy) for dx in coarse for dy in coarse), key=lambda hit: hit[0])
        fine = np.arange(-MEASURE_STEP, MEASURE_STEP + 1e-9, MEASURE_STEP / 3)
        best = max(((score(best[1] + a, best[2] + b), best[1] + a, best[2] + b) for a in fine for b in fine), key=lambda hit: hit[0])
        found, dx, dy = best
        if found < MEASURE_FOUND_SSIM or found - here < MEASURE_GAIN or max(abs(dx), abs(dy)) < MEASURE_STEP / 2:
            return None
        return round(float(dx), 3), round(float(dy), 3), round(float(found), 3)

    def _lost_finding(self, key: str, lost: dict) -> Finding:
        """The ``insert_measurement_lost`` finding of *key*: a block, naming its frame, its box and the sizes it
        was read at - its presence there, and all it said of it, unmeasured."""
        where = self.placed[key]
        frame = int(lost["frame"])
        at = max(0.0, frame / self.rate - self.offset)
        fix = KIND_FIXES[INSERT_MEASUREMENT_LOST]
        kept = ("why", "box", "read_at", "moved", "ssim", "earlier", "audited", "drawn_at")
        return Finding(
            INSERT_MEASUREMENT_LOST,
            at,
            at,
            where.scene_id,
            f"insert measurement lost at t={clock(at)}: {where.label} - {lost_words(lost)} - its presence there is not "
            f"measured - {fix}",
            FAIL,
            {
                "insert": key,
                "frame": frame,
                **{name: lost[name] for name in kept if lost.get(name) is not None},
                "span": _plan_span(frame, frame, self.rate),
                "gate": "measurement",
            },
            fix=fix,
        )

    def _check_measurements(self, size: tuple[int, int] | None) -> None:
        """An insert whose source is found at one place around the quad read on every one of its first frames read whole
        (:meth:`_looked_around`): read on another region than it is drawn on - its measurement lost on the first,
        at the *size* the gate read the encoded frames at."""
        for key, reads in self.first.items():
            if key in self.lost or not reads:
                continue
            moved = [self._looked_around(read) for read in reads]
            if any(hit is None for hit in moved):
                continue
            spread = max(max(hit[i] for hit in moved) - min(hit[i] for hit in moved) for i in (0, 1))
            if spread <= MEASURE_STEP:
                read, (dx, dy, found) = reads[0], moved[0]
                self.lost[key] = {
                    "why": "moved",
                    "frame": read.frame,
                    "box": read.box,
                    "read_at": list(size) if size else None,
                    "moved": [dx, dy],
                    "ssim": found,
                    "path": self._keep(read.frame, read.picture),
                }

    def source_index(self, key: str, frame: int) -> int:
        """The frame of *key*'s source the page shows on output *frame* (``buildInserts``: its time map, its fps)."""
        from app.services.video_studio.timeline import map_forward

        payload = self.payloads.get(key) or {}
        if payload.get("kind") != "video" or not payload.get("time_map"):
            return 0
        t = frame / self.rate - self.starts.get(key, 0.0)
        at = map_forward([tuple(point) for point in payload["time_map"]], t)
        return min(max(round(at * float(payload.get("src_fps") or 24.0)), 0), max(int(payload.get("src_frames") or 1) - 1, 0))

    def needs(self, frame: int) -> list[tuple[str, int]]:
        """``(insert, source frame)`` the reading of output *frame* compares with: the one expected, and either side."""
        out = []
        for key in self.frames.get(frame, ()):
            if frame not in self.whole.get(key, ()):
                continue
            index = self.source_index(key, frame)
            out.extend((key, n) for n in ((index - 1, index, index + 1) if self.payloads.get(key, {}).get("kind") == "video" else (0,)) if n >= 0)
        return out

    def _expected(self, key: str, picture: Any, grid: tuple[int, int], frame: int) -> tuple[Any, Any] | None:
        """``(luma, mask)`` of the source *picture* (Pillow) as its quad shows it on output *frame*, on *grid*: pushed,
        fitted as drawn - a contain fit's bands out of the mask."""
        import numpy as np
        from PIL import Image

        from app.services.video_studio.pictures import fitted

        source = self.inserts.get(key)
        payload = self.payloads.get(key) or {}
        shown = picture.convert("RGBA")
        zoom = getattr(source, "zoom", None)
        if zoom is not None:
            into = frame / self.rate - self.starts.get(key, 0.0) - float(payload.get("t0") or 0.0)
            shown = screen_insert.zoomed(shown, zoom, min(max(into, zoom.start), zoom.end))
        fit = str(getattr(source, "fit", None) or payload.get("fit") or "cover")
        shaped = fitted(shown, grid[0], grid[1], fit, pad=screen_insert.LETTERBOX).resize(grid, Image.Resampling.BILINEAR)
        rgba = np.asarray(shaped, dtype=np.float32)
        return _luma(rgba), rgba[..., 3] > 127

    def _keep(self, frame: int, picture: Any) -> str | None:
        """The encoded *frame* kept as the evidence its finding shows."""
        if self.evidence_dir is None:
            return None
        from pathlib import Path

        import numpy as np
        from PIL import Image

        folder = Path(self.evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"presence-{frame:07d}.jpg"
        if not path.exists():
            Image.fromarray(np.ascontiguousarray(picture[..., :3])).save(path, quality=90)
        return str(path)

    def _miss(
        self, key: str, frame: int, why: str, value: float | None, colour: str | None, picture: Any, alpha: float | None = None
    ) -> None:
        kept = self.missing.setdefault(key, [])
        # The first frame of each finding it may make kept as its evidence: its layer not drawn, its source missing.
        drawn = self.not_drawn(key, frame, why, alpha)
        first = not any(self.not_drawn(key, hit[0], hit[1], hit[5]) == drawn for hit in kept)
        kept.append((frame, why, value, colour, self._keep(frame, picture) if first else None, alpha))

    def not_drawn(self, key: str, frame: int, why: str, alpha: float | None) -> bool:
        """Whether *key* read missing on output *frame* (*why*, *alpha* as :meth:`_miss` keeps them) is its layer not
        drawn there: a frame the page log misses (:attr:`undrawn`), read off the encoded frame showing it whole
        - never one it placed it nowhere on, nor one of a transition or of the video's fades, nor one blending it."""
        if frame not in self.undrawn.get(key, ()) or why == "not_placed" or alpha is not None:
            return False
        spot = self._placed[key].get(frame)
        return spot is not None and placed_share(spot) >= BLEND_WHOLE and self.placed[key].blend_at(frame) is None

    def _covered(
        self, key: str, frame: int, corners: Any, grid: tuple[int, int], size: tuple[int, int]
    ) -> tuple[float, Any] | None:
        """``(the share of its quad left uncovered, where it is covered)``: what *key*'s scene draws over it on output
        *frame* (:attr:`_over`), its boxes painted on a frame of *size* and read on the quad's plane as the frame
        is (``screen_insert.rectified``) - the share on the boxes as the page gives them, the mask on *grid* grown by
        :data:`PRESENCE_COVER_GROW`. ``None`` when nothing is drawn over it there."""
        import numpy as np
        from PIL import Image, ImageDraw
        from scipy import ndimage

        boxes = self._over.get(key, {}).get(frame)
        if not boxes:
            return None
        width, height = size
        painted = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(painted)
        for x0, y0, x1, y1 in (box[:4] for box in boxes):
            draw.rectangle((float(x0) * width, float(y0) * height, float(x1) * width, float(y1) * height), fill=255)
        warped = screen_insert.rectified(np.asarray(painted), corners, grid, rim=0.0, mode="L")
        if not warped.any():
            return None
        return 1.0 - float((warped > 127).mean()), ndimage.binary_dilation(warped > 0, iterations=PRESENCE_COVER_GROW)

    def _unblended(
        self, key: str, frame: int, seen_rgb: Any, corners: Any, grid: tuple[int, int], size: tuple[int, int]
    ) -> tuple[Any, Any, float, tuple[Any, Any]] | None:
        """``(rgb, mask, alpha, toned)`` of *key*'s quad on a blended output *frame*, *seen_rgb* rectified on
        *grid*: what its scene paints there, unblended from what the frame shows beside it - the frame with the scene
        painted black is everything else, the white one less the black one how much of the scene the frame shows at
        each pixel (*alpha*: their mean over the quad); *mask* where it shows at least :data:`PRESENCE_MIN_SHOWN` of it;
        *toned* the two paintings of the whole frame (:func:`own_blocks`). ``None`` when the scene was not painted black
        and white for that frame."""
        import numpy as np
        from PIL import Image

        toned = self._blends.get(key, {}).get(frame) or {}
        if not toned.get("black") or not toned.get("white"):
            return None
        read, whole = [], []
        for tone in ("black", "white"):
            try:
                with Image.open(toned[tone]) as opened:
                    picture = opened.convert("RGB")
            except OSError:
                return None
            if picture.size != size:
                picture = picture.resize(size, Image.Resampling.BILINEAR)
            whole.append(np.asarray(picture, dtype=np.float32))
            read.append(np.asarray(screen_insert.rectified(np.ascontiguousarray(np.asarray(picture)), corners, grid, rim=0.0), dtype=np.float32))
        black, white = read
        weight = np.clip((white - black).mean(axis=2) / 255.0, 0.0, 1.0)
        rgb = np.clip((np.asarray(seen_rgb, dtype=np.float32) - black) / np.maximum(weight, 1.0 / 255.0)[..., None], 0.0, 255.0)
        return rgb, weight >= PRESENCE_MIN_SHOWN, float(weight.mean()), (whole[0], whole[1])

    def read(self, frame: int, picture: Any, sources: dict[tuple[str, int], Any] | None = None) -> None:
        """Judge encoded output *frame* (*picture*, ``H x W x 3``) for each insert it is read for; *sources* are the
        source pictures (Pillow) :meth:`needs` names."""
        import numpy as np

        picture = np.asarray(picture)
        size = (picture.shape[1], picture.shape[0])
        self.size = size
        for key in self.frames.get(frame, ()):
            source = self.inserts.get(key)
            payload = self.payloads.get(key) or {}
            whole = frame in self.whole.get(key, ())
            spot = self._placed[key].get(frame)
            # The frames its flicker and its flicker fact judge it on, and no other (Placed.owns_frame).
            owned = self.placed[key].owns_frame(frame, self._placed[key])
            lost = self.lost.get(key)
            if lost is not None:
                # Unmeasured: nothing read of it is of the region it is drawn on - its first frame of its own
                # shown whole is what its finding names and shows, cut from this decoded frame.
                if "frame" not in lost and spot is not None and whole and owned and placed_share(spot) >= BLEND_WHOLE:
                    lost.update(frame=frame, box=[round(float(v), 5) for v in spot[:4]], read_at=list(size), path=self._keep(frame, picture))
                continue
            if spot is None:
                # Shown whole there and placed on the frame beside it: the page drew no layer of it on this one.
                if whole and owned and (frame - 1 in self._placed[key] or frame + 1 in self._placed[key]):
                    self._miss(key, frame, "not_placed", None, None, picture)
                continue
            if not owned:
                # A frame of a transition that is not its scene's - the first of its transition in, the scene before
                # whole there, or one showing under the floor of it: not judged, said as a fact.
                if whole:
                    self.faint[key] = self.faint.get(key, 0) + 1
                continue
            if video_fade_left(self.document, frame / self.rate) < PRESENCE_MIN_SHOWN:
                # The video's fade from or to black leaves less of the whole frame than the floor, its insert with
                # it: nothing on it tells the insert from the black the fade lays over it - not judged, said as a fact.
                if whole:
                    self.faded[key] = self.faded.get(key, 0) + 1
                continue
            audit = self._audits[key].get(frame) or {}
            corners = _quad_on_frame(spot[:4], payload, audit, size)
            if corners is None:
                continue
            shown = placed_share(spot)
            key_spec = getattr(source, "key", None)
            if key_spec is not None and shown >= GUARD_ALONE:
                # Read inside the quad past its edge strip: the key's colour on the strip along the bezel and
                # past it is the footage's - its glow, the encoder's chroma across the bezel - as the residual key
                # judges it on the layer, never the insert gone; its surface keyed through shows on its inside.
                keyed = screen_insert.key_read(picture, corners, key_spec.key())
                own = float(audit.get("source_key_hue") or 0.0)
                if keyed.inside > own + screen_insert.RESIDUAL_FAIL_SHARE:
                    self.key_where.setdefault(key, {})[frame] = {**keyed.as_dict(), "source_key_hue": round(own, 5)}
                    self._miss(key, frame, "key", keyed.inside, None, picture)
                    continue
            if not whole or shown < PRESENCE_MIN_SHOWN:
                if whole:
                    # Most of it gone into the scene beside it: not judged, said as a fact.
                    self.faint[key] = self.faint.get(key, 0) + 1
                continue
            if float(audit.get("foreground_share") or 0.0) > PRESENCE_MAX_FOREGROUND or (payload.get("track") and not audit.get("refined")):
                self.unjudged[key] = self.unjudged.get(key, 0) + 1
                continue
            quad_w, quad_h = quad_size(tuple(tuple(point) for point in corners))
            scale = min(1.0, PRESENCE_SIDE / max(quad_w, quad_h, 1.0))
            grid = (max(16, round(quad_w * scale)), max(16, round(quad_h * scale)))
            seen_rgb = screen_insert.rectified(np.ascontiguousarray(picture[..., :3]), corners, grid, rim=0.0)
            inset = np.zeros(seen_rgb.shape[:2], dtype=bool)
            dx, dy = max(1, round(grid[0] * PRESENCE_INSET)), max(1, round(grid[1] * PRESENCE_INSET))
            inset[dy:-dy, dx:-dx] = True
            covered = self._covered(key, frame, corners, grid, size)
            if covered is not None:
                # What its scene draws over the quad is no part of the insert: read on the rest of it, when
                # enough of it is left to tell its source by.
                uncovered, over = covered
                if uncovered < PRESENCE_MIN_UNCOVERED:
                    self.covered[key] = self.covered.get(key, 0) + 1
                    continue
                inset &= ~over
            alpha = None
            toned = None
            if shown < BLEND_WHOLE:
                # A transition's frame: the insert blended with the scene beside it is read unblended - what
                # its own scene paints there - never against its raw source, which the blend is not.
                unblended = self._unblended(key, frame, seen_rgb, corners, grid, size)
                if unblended is not None:
                    seen_rgb, shows, alpha, toned = unblended
                    if alpha < PRESENCE_MIN_SHOWN:
                        # Its scene shows less of it there than the page placed: faint, not judged.
                        self.faint[key] = self.faint.get(key, 0) + 1
                        continue
                    inset &= shows
                    self.alphas.setdefault(key, {})[frame] = round(alpha, 3)
                elif shown < BLEND_RAW_SHOWN:
                    self.blended_unread[key] = self.blended_unread.get(key, 0) + 1
                    continue
            seen = _luma(seen_rgb)
            expected = []
            for (name, _n), held in (sources or {}).items():
                if name == key and held is not None:
                    read = self._expected(key, held, grid, frame)
                    if read is not None:
                        expected.append(read)
            if not expected:
                self.unjudged[key] = self.unjudged.get(key, 0) + 1
                continue
            masks = [mask & inset for _luma_of, mask in expected]
            first = self.first.setdefault(key, [])
            if alpha is None and shown >= BLEND_WHOLE and len(first) < MEASURE_FRAMES:
                # One of its first frames read whole: what the gate's own measurement is checked on.
                kept = [(luma, mask) for (luma, _m), mask in zip(expected, masks, strict=True)]
                first.append(_Read(frame, picture, [tuple(point) for point in corners], grid, kept, [round(float(v), 5) for v in spot[:4]]))
            seen_detail = detail_variance(seen, masks[0])
            scores = [s for (luma, _m), mask in zip(expected, masks, strict=True) if (s := structural_similarity(seen, luma, mask)) is not None]
            recognised = bool(scores) and max(scores) >= PRESENCE_SSIM_MIN
            if seen_detail < FLAT_VAR_MIN and max(detail_variance(luma, mask) for (luma, _m), mask in zip(expected, masks, strict=True)) >= FLAT_VAR_MIN:
                window = self.placed[key].blend_at(frame)
                if recognised:
                    # Its source's structure is there: a dark UI of dim text, dimmed further by a crossfade or an
                    # encoder, reads under the floor and is still the source - present, said as a fact, never judged
                    # against the frame around it (which a crossfade dims less than the quad it fades in).
                    self.read_frames.setdefault(key, []).append(frame)
                    if window is not None:
                        counts = self.dimmed.setdefault(key, {}).setdefault(window[2], [0, 0, 0])
                        counts[0] += 1
                        counts[2] += 1
                    else:
                        self.recognised[key] = self.recognised.get(key, 0) + 1
                    continue
                colour = _hex(np.asarray(seen_rgb, dtype=np.float32)[masks[0]].mean(axis=0))
                if window is not None:
                    # Read unblended, the scene beside it is taken away: its own layer - its quad and the frame
                    # around it - is what the transition dims or blurs, never the bright text of the scene coming in.
                    blocks = own_blocks(picture, toned, corners) if toned is not None else frame_blocks(picture, corners)
                    self.pending.setdefault(key, []).append(_Dimmed(frame, window, seen_detail, colour, blocks, picture, alpha))
                    self._settle(key)
                    _unrecord(first, frame)
                    continue
                self._miss(key, frame, "flat", seen_detail, colour, picture, alpha)
                continue
            if scores and not recognised:
                self._miss(key, frame, "absent", max(scores), None, picture, alpha)
                continue
            if not scores:
                self.unjudged[key] = self.unjudged.get(key, 0) + 1
                _unrecord(first, frame)
                continue
            self.read_frames.setdefault(key, []).append(frame)
            if frame in self.references.get(key, ()):
                self.clean.setdefault(key, {})[frame] = (seen_detail, frame_blocks(picture, corners))
                self._settle(key)

    def _settle(self, key: str, *, final: bool = False) -> None:
        """Judge *key*'s frames held inside a transition or a fade (:class:`_Dimmed`) whose scene's
        un-transitioned frame beside the window is read - all of them when *final*, with or without one."""
        kept = []
        for held in self.pending.get(key, []):
            first, end, what = held.window
            beside = [n for n in self.clean.get(key, {}) if first - PRESENCE_REFERENCE_FRAMES <= n < first or end <= n < end + PRESENCE_REFERENCE_FRAMES]
            if not beside and not final:
                kept.append(held)
                continue
            whole_frame = around_detail(held.blocks) < FLAT_VAR_MIN
            present = whole_frame
            if beside:
                detail, blocks = self.clean[key][min(beside, key=lambda n: abs(n - held.frame))]
                attenuation = frame_attenuation(held.blocks, blocks)
                if attenuation is not None:
                    present = held.detail >= PRESENCE_TRACK_SHARE * detail * attenuation
            if not present:
                self._miss(key, held.frame, "flat", held.detail, held.colour, held.picture, held.alpha)
                continue
            self.read_frames.setdefault(key, []).append(held.frame)
            counts = self.dimmed.setdefault(key, {}).setdefault(what, [0, 0, 0])
            counts[0] += 1
            counts[1] += int(whole_frame)
        self.pending[key] = kept

    def result(self) -> Presence:
        """The findings and what each insert's report says (:class:`Presence`)."""
        for key in list(self.pending):
            self._settle(key, final=True)
        self._check_measurements(self.size)
        out = Presence()
        # The measure is deterministic: the same spec draws the same frame, so rendering again changes nothing.
        # Frames all inside one transition are kept off by a spec change (:func:`join_action`); others are a fact.
        unexplained = (
            "nothing in the spec causes it and rendering again draws the same frame: report the render with this frame - "
            "an insert that runs to its scene's end is shown to the scene's last frame"
        )
        for key, lost in self.lost.items():
            if "frame" not in lost:
                # Shown whole on none of the frames read here: nothing of it is measured, nothing is said of it.
                continue
            # Its measurement lost: a block of its own, never its readings of another region.
            out.by_key[key] = {"frames": 0, "missing": 0, "unjudged": 0, "joins": 0, "joins_missing": 0, "lost": dict(lost)}
            out.findings.append((self._lost_finding(key, lost), lost.get("path")))
        for key, where in self.placed.items():
            if key in self.lost:
                continue
            read = self.read_frames.get(key, [])
            missing = sorted(self.missing.get(key, []), key=lambda hit: hit[0])
            measured = len(read) + len(missing)
            if not measured and not any(
                key in counted for counted in (self.unjudged, self.blended_unread, self.faint, self.faded, self.covered)
            ):
                continue
            # Its layer not drawn: the page log's misses the encoded frame reads it missing on, shown whole.
            gone = [hit for hit in missing if self.not_drawn(key, hit[0], hit[1], hit[5])]
            lost = [hit for hit in missing if hit not in gone]
            on_joins = [frame for frame in read if where.at_join(frame)]
            missed_joins = [hit for hit in missing if where.at_join(hit[0])]
            alphas = self.alphas.get(key, {})
            summary = {
                "frames": measured,
                "missing": len(missing),
                "unjudged": self.unjudged.get(key, 0),
                "joins": len(on_joins) + len(missed_joins),
                "joins_missing": len(missed_joins),
            }
            if alphas:
                # Its blended frames read unblended: how many, and the least alpha the insert had on one.
                summary["blended"] = len(alphas)
                summary["least_alpha"] = min(alphas.values())
            if self.blended_unread.get(key):
                summary["blended_unread"] = self.blended_unread[key]
            if self.faint.get(key):
                summary["faint"] = self.faint[key]
            if self.faded.get(key):
                summary["faded"] = self.faded[key]
            if self.covered.get(key):
                summary["covered"] = self.covered[key]
            if gone:
                summary["undrawn"] = len(gone)
            if missing:
                summary["first_missing"] = round(max(0.0, missing[0][0] / self.rate - self.offset), 3)
            if on_joins or missed_joins:
                # Where on its joins it was read present and missing, in the plan: what another drawing of the same join
                # is held to (:func:`agree_on_joins`).
                summary["join_spans"] = {
                    "present": [_plan_span(a, b, self.rate) for a, b in _runs(on_joins)],
                    "missing": [_plan_span(a, b, self.rate) for a, b in _runs([hit[0] for hit in missed_joins])],
                }
            if self.recognised.get(key):
                summary["recognised"] = self.recognised[key]
            if dimmed := self.dimmed.get(key):
                summary["dimmed"] = [
                    {"window": what, "frames": frames, "whole_frame": whole, "recognised": known}
                    for what, (frames, whole, known) in dimmed.items()
                ]
            out.by_key[key] = summary
            if gone:
                out.findings.append(self._not_drawn_finding(key, where, gone))
            if not lost:
                continue
            frame, why, value, colour, _path, alpha = lost[0]
            path = next((hit[4] for hit in lost if hit[4]), None)
            lost_joins = [hit for hit in lost if where.at_join(hit[0])]
            chosen = join_action(self.document, where, [hit[0] for hit in lost], fps=self.rate) or action(NOTHING, unexplained)
            fix = chosen["text"]
            words = self._missing_words(where, lost[0])
            # On a transition's frame, the insert's alpha there: a real loss and a blend are told apart.
            blend = f" (the insert at alpha {alpha:.2f} there, blended with the scene beside it)" if alpha is not None else ""
            at = max(0.0, frame / self.rate - self.offset)
            out.findings.append(
                (
                    Finding(
                        INSERT_MISSING,
                        at,
                        max(0.0, lost[-1][0] / self.rate - self.offset),
                        where.scene_id,
                        f"insert missing at t={clock(at)}: {where.label} - {words} on frame {frame} of the encoded video"
                        f"{blend}, which it shows whole on ({len(lost)} of {measured} frame{'s' if measured != 1 else ''} "
                        f"read there missing it{', on its scene' + chr(39) + 's join with another' if lost_joins else ''}) - {fix}",
                        FAIL,
                        {
                            "insert": key,
                            "frame": frame,
                            "frames": len(lost),
                            "measured": measured,
                            "why": why,
                            **({"value": round(float(value), 5)} if value is not None else {}),
                            **({"colour": colour} if colour else {}),
                            **({"alpha": round(float(alpha), 3)} if alpha is not None else {}),
                            **({"key_where": placed_where} if (placed_where := self.key_where.get(key, {}).get(frame)) else {}),
                            "transition": bool(lost_joins),
                            "span": _plan_span(frame, lost[-1][0], self.rate),
                            "gate": "presence",
                            "threshold": {"flat": FLAT_VAR_MIN, "absent": PRESENCE_SSIM_MIN}.get(why, screen_insert.RESIDUAL_FAIL_SHARE),
                            "action": chosen,
                        },
                        fix=fix,
                    ),
                    path,
                )
            )
        return out

    def _missing_words(self, where: Placed, hit: tuple) -> str:
        """What the encoded frame of a frame read missing (*hit*, as :meth:`_miss` keeps it) shows of its insert."""
        frame, why, value, colour, _path, alpha = hit
        against = "the source frame expected" if alpha is None else "the source frame expected, the frame unblended"
        words = {
            "flat": f"its quad is one flat colour ({colour}, detail variance {float(value or 0.0):.1f} under {FLAT_VAR_MIN:g})",
            "absent": f"its source is not on its quad (SSIM {float(value or 0.0):.2f} under {PRESENCE_SSIM_MIN:g} against {against})",
            "key": f"its surface shows the key's colour on {float(value or 0.0) * 100:.2f}% of its quad"
            + key_where_text(self.key_where.get(where.key, {}).get(frame)),
            "not_placed": "the page placed no layer of it",
        }[why]
        if why == "flat" and (window := where.blend_at(frame)) is not None:
            words += f", inside the {window[2]}: flat against the frame around it, which the transition dims or blurs less"
        return words

    def _not_drawn_finding(self, key: str, where: Placed, gone: list[tuple]) -> tuple[Finding, str | None]:
        """The frames *gone* (:meth:`not_drawn`): the page draws *key*'s layer there, the frame server drew
        none, and the encoded frame, showing it whole, reads it missing - with that frame. On one frame ``insert
        flickers``, the harness's (``render_checks.HARNESS_MAX_FRAMES``); on more, ``insert missing``."""
        frames = [hit[0] for hit in gone]
        first, last, why = frames[0], frames[-1], gone[0][1]
        joined = [frame for frame in frames if where.at_join(frame)]
        chosen = action(NOTHING, "nothing in the video causes it: render again, and report the render if it stays")
        on_joins = f", {len(joined)} on its scene's join with another" if joined else ""
        named = (
            f"frames {', '.join(str(n) for n in frames[:-1])} and {last}" if 1 < len(frames) <= MAX_NAMED_FRAMES else f"frame {first}"
        )
        lost = len(frames) > HARNESS_MAX_FRAMES
        at = max(0.0, first / self.rate - self.offset)
        finding = Finding(
            INSERT_MISSING if lost else INSERT_FLICKER,
            at,
            max(0.0, last / self.rate - self.offset),
            where.scene_id,
            f"insert {'missing' if lost else 'flickers'} at t={clock(at)}: {where.label} is not drawn on {named}, where it is drawn on the frames "
            f"around it - {self._missing_words(where, gone[0])} on frame {first} of the encoded video, which shows it "
            f"whole, and the page drew no layer of it there ({len(gone)} frame{'s' if len(gone) != 1 else ''} without "
            f"it{on_joins}) - {chosen['text']}",
            FAIL,
            {
                "insert": key,
                "frame": first,
                "absent": len(gone),
                "missed": frames[:MAX_NAMED_FRAMES],
                "span": _plan_span(first, last, self.rate),
                "first_at": round(at, 3),
                **({"transition": len(joined)} if joined else {}),
                "why": why,
                "gate": "flicker",
                "value": len(gone),
                "threshold": 0,
                "excluded": {},
                "action": chosen,
            },
            fix=chosen["text"],
        )
        return finding, next((hit[4] for hit in gone if hit[4]), None)


class _SourceFrames:
    """The source frames the presence gate compares with: an image insert's picture once, a video insert's
    frames decoded forward at most :data:`PRESENCE_SOURCE_SIDE` long, the last few kept. A video whose first frame
    wanted is :data:`PRESENCE_SOURCE_SEEK_FRAMES` or more in is opened by a seek just before it (issue #9331): a gate
    resumed late in a render never decodes the source from its start in one step its watchdog takes for a stall.
    *on_source* is called for each source frame decoded."""

    def __init__(
        self, inserts: dict[str, Any], payloads: dict[str, dict], on_source: Callable[[], None] | None = None
    ) -> None:
        self.inserts, self.payloads, self.on_source = inserts, payloads, on_source
        self.pictures: dict[str, Any] = {}
        self.streams: dict[str, Any] = {}
        self.read: dict[str, int] = {}
        self.kept: dict[str, dict[int, Any]] = {}
        self.ended: set[str] = set()

    async def get(self, wanted: list[tuple[str, int]]) -> dict[tuple[str, int], Any]:
        import asyncio

        from PIL import Image

        from app.services.video_studio import encoder

        out: dict[tuple[str, int], Any] = {}
        for key, index in wanted:
            source = self.inserts.get(key)
            if source is None:
                continue
            if getattr(source, "kind", None) != "video":
                if key not in self.pictures:
                    try:
                        self.pictures[key] = await asyncio.to_thread(screen_insert.read_picture, source.path)
                    except (OSError, ValueError):
                        self.pictures[key] = None
                out[(key, index)] = self.pictures[key]
                continue
            kept = self.kept.setdefault(key, {})
            if key not in self.streams and key not in self.ended:
                payload = self.payloads.get(key) or {}
                w, h = int(payload.get("src_w") or 0), int(payload.get("src_h") or 0)
                scale = min(1.0, PRESENCE_SOURCE_SIDE / max(w, h, 1))
                width, height = max(2, round(w * scale / 2) * 2), max(2, round(h * scale / 2) * 2)
                if w <= 0 or h <= 0:
                    self.ended.add(key)
                else:
                    # The gate compares with the index and the frame before: the seek lands on the one before.
                    begin = max(0, index - 1) if index >= PRESENCE_SOURCE_SEEK_FRAMES else 0
                    rate = max(float(payload.get("src_fps") or 24.0), 1.0)
                    start = (begin - 0.5) / rate if begin else 0.0
                    frames = encoder.iter_rgb_frames(source.path, width=width, height=height, resize=True, start=start)
                    self.streams[key] = (frames, (width, height))
                    self.read[key] = begin
            while key in self.streams and self.read[key] <= index:
                stream, (width, height) = self.streams[key]
                try:
                    raw = await anext(stream)
                except (StopAsyncIteration, encoder.EncoderError):
                    await self._close(key)
                    break
                kept[self.read[key]] = Image.frombytes("RGB", (width, height), raw)
                self.read[key] += 1
                if self.on_source is not None:
                    self.on_source()
                for old in [n for n in kept if n < index - 2]:
                    kept.pop(old)
            out[(key, index)] = kept.get(index)
        return out

    async def _close(self, key: str) -> None:
        stream, _size = self.streams.pop(key)
        self.ended.add(key)
        await stream.aclose()

    async def close(self) -> None:
        for key in list(self.streams):
            await self._close(key)


#: The presence gate keeps its place (:class:`PresencePlace`) after at least this many encoded frames read since the
#: last one, at a frame no transition holds back: a relaunch past the draw goes on from there, never from frame 0.
PRESENCE_PLACE_FRAMES = 600
#: ... or after this many wall-clock seconds with a frame judged since the last one: a slow pass (a resumed one near
#: the render's end) keeps its place before a deploy cuts it, so the next relaunch goes on from a later frame.
PRESENCE_PLACE_SECONDS = 60.0
#: A video insert whose first source frame wanted is this far in is opened by a seek (:class:`_SourceFrames`).
PRESENCE_SOURCE_SEEK_FRAMES = 48
#: The wall-clock seconds the copy of that place beside the render may take: a slow store costs that copy only.
PRESENCE_PLACE_STORE_SECONDS = 30.0


class PresenceHandedOverError(Exception):
    """The presence gate stopped because its worker hands the render over (``compositor.handing_over``): its place was
    kept at the frame it reached, so the next attempt goes on from there. Never a cancel, never a failure."""

    def __init__(self, frame: int, kept: bool) -> None:
        self.frame, self.kept = frame, kept
        said = f"its place kept at frame {frame}" if kept else f"at frame {frame}, its last place kept before"
        super().__init__(f"The read-back of the screen inserts was handed over: {said}.")


@dataclass(frozen=True)
class PresencePlace:
    """Where the presence gate over one encoded video keeps what it judged (issue #8958): read back only by a gate
    over the same video, read the same way (*key*), which skips the frames already judged and goes on from there.
    *stored* is called with :attr:`path` after each save: the render keeps it beside itself, so an attempt on
    another worker - a deploy replaced this one and its disk - goes on from there too. Best effort."""

    path: Any
    key: str
    stored: Callable[[Any], Awaitable[None]] | None = None


def _plain(value: Any) -> Any:
    """A numpy scalar or array kept in a place's JSON as the number or list it holds."""
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not kept in a presence place")


def _int_keyed(table: dict) -> dict:
    return {int(frame): value for frame, value in table.items()}


def gate_state(gate: PresenceGate, next_frame: int) -> tuple[dict, dict[str, Any]]:
    """``(state, arrays)``: what *gate* judged before *next_frame*, JSON and the pictures it still reads at its end
    (its first frames read whole, the clean frames beside its windows). Only at a frame no transition holds back
    (:attr:`PresenceGate.pending` empty)."""
    import numpy as np

    arrays: dict[str, Any] = {}

    def put(value: Any) -> str:
        name = f"a{len(arrays)}"
        arrays[name] = np.asarray(value)
        return name

    state = {
        "next": int(next_frame),
        "size": list(gate.size) if gate.size else None,
        "covered": gate.covered,
        "read_frames": gate.read_frames,
        "unjudged": gate.unjudged,
        "alphas": gate.alphas,
        "blended_unread": gate.blended_unread,
        "faint": gate.faint,
        "faded": gate.faded,
        "missing": {key: [list(hit) for hit in hits] for key, hits in gate.missing.items()},
        "key_where": gate.key_where,
        "dimmed": gate.dimmed,
        "recognised": gate.recognised,
        "lost": gate.lost,
        "clean": {
            key: {str(frame): [float(detail), put(blocks)] for frame, (detail, blocks) in frames.items()}
            for key, frames in gate.clean.items()
        },
        "first": {
            key: [
                {
                    "frame": read.frame,
                    "picture": put(read.picture),
                    "corners": [list(point) for point in read.corners],
                    "grid": list(read.grid),
                    "expected": [[put(luma), put(mask)] for luma, mask in read.expected],
                    "box": list(read.box),
                }
                for read in reads
            ]
            for key, reads in gate.first.items()
        },
    }
    return state, arrays


def gate_restore(gate: PresenceGate, state: dict, arrays: Any) -> int:
    """*gate* given back what a gate over the same video judged (:func:`gate_state`); the frame to go on from.
    An evidence frame it names that this disk lacks - kept by a worker gone - is dropped: its finding stays."""
    from pathlib import Path

    def held(path: Any) -> Any:
        return path if path and Path(str(path)).is_file() else None

    gate.size = tuple(state["size"]) if state.get("size") else None
    gate.covered = dict(state.get("covered") or {})
    gate.read_frames = {key: [int(n) for n in frames] for key, frames in (state.get("read_frames") or {}).items()}
    gate.unjudged = dict(state.get("unjudged") or {})
    gate.alphas = {key: _int_keyed(table) for key, table in (state.get("alphas") or {}).items()}
    gate.blended_unread = dict(state.get("blended_unread") or {})
    gate.faint = dict(state.get("faint") or {})
    gate.faded = dict(state.get("faded") or {})
    gate.missing = {
        key: [(int(hit[0]), hit[1], hit[2], hit[3], held(hit[4]), hit[5]) for hit in hits]
        for key, hits in (state.get("missing") or {}).items()
    }
    gate.key_where = {key: _int_keyed(table) for key, table in (state.get("key_where") or {}).items()}
    gate.dimmed = {key: dict(table) for key, table in (state.get("dimmed") or {}).items()}
    gate.recognised = dict(state.get("recognised") or {})
    gate.lost = {
        key: {**record, "path": held(record["path"])} if "path" in record else dict(record)
        for key, record in (state.get("lost") or {}).items()
    }
    gate.clean = {
        key: {int(frame): (float(detail), arrays[name]) for frame, (detail, name) in frames.items()}
        for key, frames in (state.get("clean") or {}).items()
    }
    gate.first = {
        key: [
            _Read(
                int(read["frame"]),
                arrays[read["picture"]],
                [tuple(point) for point in read["corners"]],
                (int(read["grid"][0]), int(read["grid"][1])),
                [(arrays[luma], arrays[mask]) for luma, mask in read["expected"]],
                list(read["box"]),
            )
            for read in reads
        ]
        for key, reads in (state.get("first") or {}).items()
    }
    gate.pending = {}
    return int(state["next"])


def _place_key(place: PresencePlace, placed: dict[str, Placed], unmeasured: dict[str, dict] | None) -> str:
    """*place*'s key and what this gate reads: the inserts it judges and the ones it leaves unmeasured."""
    said = json.dumps([place.key, sorted(placed), unmeasured or {}], sort_keys=True, default=str)
    return hashlib.sha256(said.encode("utf-8")).hexdigest()


def load_place(place: PresencePlace, key: str) -> tuple[dict, dict[str, Any]] | None:
    """The state *place* keeps for *key* (:func:`save_place`), ``None`` for none, another video's or unreadable."""
    import numpy as np

    try:
        with np.load(place.path, allow_pickle=False) as kept:
            state = json.loads(str(kept["state"]))
            if not isinstance(state, dict) or state.get("key") != key:
                return None
            return state, {name: kept[name] for name in kept.files if name != "state"}
    except (OSError, ValueError, KeyError):
        return None


def save_place(place: PresencePlace, key: str, state: dict, arrays: dict[str, Any]) -> bool:
    """Write *state* and *arrays* whole to *place*, then over the last one: a process stopped mid-write leaves the
    place before. Whether it was written."""
    import os
    from pathlib import Path

    import numpy as np

    path = Path(place.path)
    partial = path.with_name(path.name + ".part.npz")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        said = json.dumps({"key": key, **state}, default=_plain)
        np.savez(partial, state=np.array(said), **arrays)
        os.replace(partial, path)
        return True
    except (OSError, ValueError, TypeError):
        return False


async def _store_place(place: PresencePlace) -> None:
    import asyncio

    if place.stored is None:
        return
    try:
        await asyncio.wait_for(place.stored(place.path), PRESENCE_PLACE_STORE_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # best effort: the next place goes again
        logging.getLogger(__name__).info("Presence gate: its place not stored beside the render: %s", format_exception_message(exc))


async def presence(
    video_path: str,
    audits: dict[str, dict],
    placed: dict[str, Placed],
    document: dict,
    inserts: dict[str, Any],
    *,
    fps: float,
    size: tuple[int, int],
    workdir: str | Any,
    first_frame: int = 0,
    offset: float = 0.0,
    frame_range: tuple[int, int] | None = None,
    on_frame: Callable[[], None] | None = None,
    unmeasured: dict[str, dict] | None = None,
    place: PresencePlace | None = None,
    handing_over: Callable[[], bool] | None = None,
    resumed: Callable[[int], None] | None = None,
    on_source: Callable[[], None] | None = None,
) -> Presence:
    """The presence gate over a render's or a preview's own encoded *video_path* (:class:`PresenceGate`): its
    frames decoded forward at most :data:`PRESENCE_WIDTH` wide (*size* the video's), each one :func:`presence_frames`
    names judged. *first_frame* is the video's first output frame and *offset* its start in the plan (a scene preview's
    window). *on_frame* is called for each frame decoded: the render's lease and its stall window see it move.
    *unmeasured* are the inserts the render drew without the bezel earlier measures found (:func:`unmeasured`):
    their measurement lost, never read. A whole render and a scene preview read alike: one decoder, the frame's size,
    every box 0-1 of it. *place* keeps where it stands every :data:`PRESENCE_PLACE_FRAMES` frames: a gate over the
    same video that finds it decodes the frames already judged without reading them (*on_frame* still called for
    each) and goes on from there - a relaunch past the draw never reads the video again from its start; *resumed* is
    told the frame it goes on from. *handing_over* says its worker hands the render over: the gate keeps its place at
    the first frame no transition holds back, stores it and stops (:class:`PresenceHandedOverError`), so the next
    attempt judges none of the frames this one judged. The place is also kept every :data:`PRESENCE_PLACE_SECONDS`
    with a frame judged since (issue #9331), and *on_source* is called for each frame of a video insert's source
    decoded, so a working source catch-up is never taken for a stall."""
    import asyncio
    import time
    from pathlib import Path

    import numpy as np

    from app.services.video_studio import encoder

    gate = PresenceGate(
        audits,
        placed,
        document,
        inserts,
        fps=fps,
        offset=offset,
        frame_range=frame_range,
        evidence_dir=Path(workdir) / "presence",
        unmeasured=unmeasured,
    )
    wanted = sorted(frame for frame in gate.frames if frame >= first_frame)
    if not wanted:
        return gate.result()
    key = _place_key(place, placed, unmeasured) if place is not None else ""
    kept = await asyncio.to_thread(load_place, place, key) if place is not None else None
    resume = gate_restore(gate, *kept) if kept is not None else first_frame
    if kept is not None and resumed is not None:
        resumed(resume)
    width = min(int(size[0]), PRESENCE_WIDTH) // 2 * 2
    height = max(2, round(width * int(size[1]) / max(int(size[0]), 1) / 2) * 2)
    sources = _SourceFrames(inserts, gate.payloads, on_source)
    frame = first_frame
    since = 0
    kept_at = time.monotonic()
    try:
        async for raw in encoder.iter_rgb_frames(video_path, width=width, height=height, resize=True):
            if frame > wanted[-1]:
                break
            if frame >= resume and frame in gate.frames:
                picture = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                await asyncio.to_thread(gate.read, frame, picture, await sources.get(gate.needs(frame)))
                since += 1
            frame += 1
            if on_frame is not None:
                on_frame()
            stopping = handing_over is not None and handing_over()
            if stopping and frame <= resume and kept is not None:
                # Still decoding past the frames an attempt before judged: the place kept stays the one to go on from.
                raise PresenceHandedOverError(resume, True)
            if stopping and any(gate.pending.values()):
                # A transition holds its frames back: read on to its end, the hand-over's close window bounds it.
                continue
            due = since >= PRESENCE_PLACE_FRAMES or (since > 0 and time.monotonic() - kept_at >= PRESENCE_PLACE_SECONDS)
            if place is not None and (stopping or due) and not any(gate.pending.values()):
                since, kept_at = 0, time.monotonic()
                state, arrays = gate_state(gate, frame)
                saved = await asyncio.to_thread(save_place, place, key, state, arrays)
                if saved:
                    await _store_place(place)
                if stopping:
                    raise PresenceHandedOverError(frame, saved)
            elif stopping:
                raise PresenceHandedOverError(frame, False)
    finally:
        await sources.close()
    return gate.result()


def _meeting(spans: list, others: list) -> list[list]:
    """The spans of *spans* (``[from, to]`` in the plan) one of *others* meets, to ``render_checks.SAME_FRAMES_SECONDS``."""
    return [
        span
        for span in spans
        if any(float(span[0]) <= float(o[1]) + SAME_FRAMES_SECONDS and float(o[0]) <= float(span[1]) + SAME_FRAMES_SECONDS for o in others)
    ]


def _within(span: list, spans: list) -> bool:
    """Whether *span* lies inside one of *spans* (plan seconds), to ``render_checks.SAME_FRAMES_SECONDS``."""
    return any(float(s[0]) - SAME_FRAMES_SECONDS <= float(span[0]) and float(span[1]) <= float(s[1]) + SAME_FRAMES_SECONDS for s in spans)


def agree_on_joins(found: Presence, earlier: dict[str, list[dict]], placed: dict[str, Placed], *, offset: float = 0.0) -> list[str]:
    """*found* held to what earlier drawings of the same joins read of each insert (*earlier*,
    ``render_checks.join_readings``). Two drawings of one join - the preview of each scene beside it, a whole render -
    read the same frames of the plan: where one reads an insert present and the other missing, the measure sits on its
    threshold there and flips with the encode. That is said, never warned on: the notes returned, ``disagrees`` on the
    insert's summary, and a presence finding whose every missing frame is on a join an earlier drawing read it present
    on is taken out of *found*."""
    notes: list[str] = []
    dropped: set[str] = set()
    for key, summary in found.by_key.items():
        mine = summary.get("join_spans") or {}
        for record in earlier.get(key) or []:
            lost_here = _meeting(mine.get("missing") or [], record.get("present") or [])
            lost_there = [] if lost_here else _meeting(mine.get("present") or [], record.get("missing") or [])
            if not lost_here and not lost_there:
                continue
            here, there = ("missing", "present") if lost_here else ("present", "missing")
            at = round(max(0.0, float((lost_here or lost_there)[0][0]) - offset), 3)
            named = f"{record.get('stage') or 'preview'} {record.get('id')}"
            summary["disagrees"] = {"stage": record.get("stage"), "id": record.get("id"), "at": at, "here": here}
            whole = int(summary.get("missing") or 0) == int(summary.get("joins_missing") or 0)
            if lost_here and whole and all(_within(span, record.get("present") or []) for span in mine["missing"]):
                dropped.add(key)
            label = placed[key].label if key in placed else key
            said = f"{label}: read {here} on its scene's join at t={clock(at)}, where {named} of the same join read it {there}"
            if lost_here:
                said += ", not a finding" if key in dropped else "; its other missing frames stay a finding"
            notes.append(f"{said} - the two drawings disagree on a measure at its threshold there.")
            break
    found.findings = [hit for hit in found.findings if not (hit[0].data.get("gate") == "presence" and hit[0].data.get("insert") in dropped)]
    return notes


def overruns(audits: dict[str, dict], placed: dict[str, Placed], document: dict, *, fps: float, offset: float = 0.0) -> list[Finding]:
    """An ``insert_missing`` finding for each insert that runs to its scene's end whose scene a frame painted at or
    past its painted end, or asked partly shown once in (``overrun`` in *audits*, the page's and the frame
    server's - ``timeline.whole_frames``): naming the scene, the second, its length and its painted end."""
    from app.services.video_studio import timeline

    rate = max(float(fps), 1.0)
    scenes = {
        str(payload.get("src")): (scene, payload)
        for scene in document.get("scenes") or []
        for payload in scene.get("inserts") or []
        if isinstance(payload, dict)
    }
    out: list[Finding] = []
    for key, where in placed.items():
        found = {int(n): record for n, record in ((audits.get(key) or {}).get("overrun") or {}).items()}
        if not found or key not in scenes:
            continue
        scene, payload = scenes[key]
        start = float(scene.get("start") or 0.0)
        length = round(float(scene.get("end") or 0.0) - start, 4)
        end = round(timeline.painted_end(payload), 4)
        frame = min(found)
        record = found[frame]
        t = float(record.get("t", frame / rate - start))
        at = max(0.0, frame / rate - offset)
        if record.get("why") == "partly_shown":
            what = (
                f"drawn {float(record.get('visibility') or 0.0):.0%} shown at t={t:.3f} s of its scene, inside the span it "
                "shows whole on"
            )
        else:
            what = f"its scene's footage painted at t={t:.3f} s of the scene, at or past its painted end"
        fix = "nothing in the spec causes it and rendering again draws the same frame: report the render with this frame"
        out.append(
            Finding(
                INSERT_MISSING,
                at,
                max(0.0, max(found) / rate - offset),
                where.scene_id,
                f"insert missing at t={clock(at)}: {where.label} runs to its scene's end, and frame {frame} has {what} "
                f"(scene {scene.get('id')}, length {length:.3f} s, painted end {end:.3f} s; {len(found)} frame"
                f"{'s' if len(found) != 1 else ''}) - {fix}",
                FAIL,
                {
                    "insert": key,
                    "frame": frame,
                    "frames": len(found),
                    "span": _plan_span(frame, max(found), rate),
                    "scene_t": round(t, 4),
                    "length": length,
                    "painted_end": end,
                    "why": str(record.get("why") or "painted_past"),
                    "gate": "painted_end",
                    "action": action(NOTHING, fix),
                },
                fix=fix,
            )
        )
    return out


#: Two inserts of one scene on one surface are back to back when the next comes in at most this long after the first
#: goes (seconds) - a longer wait is an empty screen the spec asks for ...
BACK_TO_BACK_SECONDS = 0.5
#: ... and a frame of their join is empty when neither shows past this share (``timeline.insert_visibility``): the
#: surface mostly bare between the first's fade out and the next's fade in - two inserts cut back to back with no overlap
#: dip under it for a tenth of a second; overlapped by a fade, one of them always shows half or more.
GAP_MAX_VISIBILITY = 0.25
#: An empty join of this many frames or more fails (``insert_gap``); a shorter one is a warning.
GAP_FAIL_FRAMES = 2


def _same_surface(a: dict, b: dict) -> bool:
    """Whether two insert payloads of a scene are drawn on one surface: the surface the plan hands over
    (``timeline.hold_surfaces``), else quads whose bounds meet."""
    if a.get("surface") is not None and b.get("surface") is not None:
        return a["surface"] == b["surface"]
    try:
        ax, ay = [float(p[0]) for p in a["quad"]], [float(p[1]) for p in a["quad"]]
        bx, by = [float(p[0]) for p in b["quad"]], [float(p[1]) for p in b["quad"]]
    except (KeyError, TypeError, ValueError, IndexError):
        return False
    return min(ax) < max(bx) and min(bx) < max(ax) and min(ay) < max(by) and min(by) < max(ay)


def insert_gaps(
    document: dict,
    placed: dict[str, Placed] | None = None,
    *,
    fps: float,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
) -> list[Finding]:
    """An ``insert_gap`` finding for each join of two inserts of a scene placed back to back on one surface
    (:data:`BACK_TO_BACK_SECONDS`) whose output frames show neither (:data:`GAP_MAX_VISIBILITY`): the surface bare
    between the first going and the next coming in, read frame by frame on the plan (``timeline.insert_visibility``,
    what the page and the frame server draw). Only the inserts of *placed* when given, only the frames of *span*
    (``(start, duration)`` in seconds, a scene preview's window)."""
    from app.services.video_studio import timeline

    rate = max(float(fps), 1.0)
    out: list[Finding] = []
    for scene in document.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
        payloads = [p for p in scene.get("inserts") or [] if isinstance(p, dict) and (placed is None or str(p.get("src")) in placed)]
        ordered = sorted(payloads, key=lambda p: float(p.get("t0") or 0.0))
        for index, first in enumerate(ordered):
            if first.get("to_end"):
                continue
            t1 = float(first.get("t1") or 0.0)
            after = [p for p in ordered[index + 1 :] if _same_surface(first, p) and float(p.get("t0") or 0.0) >= t1 - INSERT_FADE_SECONDS]
            if not after:
                continue
            nxt = after[0]
            t0 = float(nxt.get("t0") or 0.0)
            if t0 - t1 > BACK_TO_BACK_SECONDS:
                continue
            low = math.floor((start + t1 - INSERT_FADE_SECONDS) * rate)
            high = math.ceil((start + min(t0 + INSERT_FADE_SECONDS, end - start)) * rate)
            empty = []
            for n in range(max(low, 0), high + 1):
                t = n / rate - start
                if t < 0 or start + t >= end:
                    continue
                if span is not None and not (span[0] <= n / rate < span[0] + span[1]):
                    continue
                shown = max(timeline.insert_visibility(first, t), timeline.insert_visibility(nxt, t))
                if shown <= GAP_MAX_VISIBILITY:
                    empty.append(n)
            if not empty:
                continue
            run = longest_run(empty) or (empty[0], empty[-1])
            key, before = str(nxt.get("src") or ""), str(first.get("src") or "")
            label = placed[key].label if placed and key in placed else key
            at = max(0.0, empty[0] / rate - offset)
            fix = (
                f"start the next insert at least {INSERT_FADE_SECONDS:g} s before the first ends (overlap them), or "
                "cross-dissolve between the two sources, so the surface is never shown bare"
            )
            out.append(
                Finding(
                    INSERT_GAP,
                    at,
                    max(0.0, empty[-1] / rate - offset),
                    str(scene.get("id") or ""),
                    f"surface empty at t={clock(at)}: between {before} and {label}, back to back on one surface, "
                    f"{len(empty)} frame{'s' if len(empty) != 1 else ''} show neither (frames {run[0]}-{run[1]}; the first "
                    f"ends at {t1:.3f} s of its scene, the next comes in at {t0:.3f} s) - {fix}",
                    FAIL if len(empty) >= GAP_FAIL_FRAMES else WARN,
                    {
                        "insert": key,
                        "previous": before,
                        "frame": empty[0],
                        "frames": len(empty),
                        "run": [run[0], run[1]],
                        "span": _plan_span(empty[0], empty[-1], rate),
                        "gate": "insert_gap",
                        "action": action(OVERLAP_INSERTS, fix),
                    },
                    fix=fix,
                )
            )
    return out


def _plan_span(first: int, last: int, rate: float) -> list[float]:
    """``[from, to]`` in seconds of the plan of the output frames *first* to *last*: where a finding that nothing in the
    spec causes sits whatever MP4 drew it (``render_checks.harness_defects``)."""
    return [round(first / rate, 3), round(last / rate, 3)]


def longest_run(frames: list[int]) -> tuple[int, int] | None:
    """The longest run of consecutive output frames of *frames* as ``(first, last)``, the earliest of equal ones; ``None``
    for none."""
    runs = _runs(frames)
    return max(runs, key=lambda run: (run[1] - run[0], -run[0])) if runs else None


def _runs(frames: list[int]) -> list[tuple[int, int]]:
    """Consecutive output frames, as ``(first, last)`` runs."""
    runs: list[tuple[int, int]] = []
    for frame in sorted(frames):
        if runs and frame == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], frame)
        else:
            runs.append((frame, frame))
    return runs


#: The fields of a frame's audit that are the bezel finder's reading on it (:func:`bezel_readings`): set aside
#: together, the frame then read as one where no bezel was found - the bezel read against a static insert's lock
#: (``alignment_drift``) among them.
BEZEL_READING_FIELDS = ("alignment", "alignment_found", "alignment_sides", "alignment_drift", "drift_sides", "bezel_found")


@dataclass(frozen=True)
class StoredBezel:
    """How far off its bezel a drawing before this render read an insert's quad (:func:`stored_bezel`)."""

    #: The quad off its bezel, mean over its corners, a share of the frame's width.
    alignment: float
    #: The size of the frames it was read on, ``(w, h)``.
    size: tuple[int, int]
    #: What read it: ``save revision 9``, ``preview <render id>``.
    by: str
    #: The frame the save read: ``frame 0 (0.00 s) of its footage``; ``None`` for a drawing, read on its own frames.
    where: str | None = None

    def words(self) -> str:
        """``0.02% of the frame's width off its bezel (mean over its corners), as save revision 9 measured it on frame 0
        (0.00 s) of its footage at 1920x1080``."""
        on = f" on {self.where}" if self.where else ""
        return (
            f"{self.alignment * 100:.2f}% of the frame's width off its bezel (mean over its corners), as {self.by} "
            f"measured it{on} at {self.size[0]}x{self.size[1]}"
        )

    def as_dict(self) -> dict:
        return {
            "alignment": round(self.alignment, 5),
            "measured_at": list(self.size),
            "by": self.by,
            **({"where": self.where} if self.where else {}),
        }


def stored_bezel(records: list[dict]) -> StoredBezel | None:
    """The last measure of an insert's bezel kept before this render: the first of *records*
    (:func:`passed_records`) that says how far off it its quad was and at what size - its own scene's drawing as it is
    now (``render_checks.insert_verdicts``) before its save's (``insert_bezels.verdicts``); ``None`` when none says."""
    for record in records:
        size = record.get("measured_at")
        if record.get("alignment") is None or not isinstance(size, list) or len(size) != 2:
            continue
        by = f"{record.get('stage') or 'drawing'} {record.get('id') or ''}".strip()
        return StoredBezel(float(record["alignment"]), (int(size[0]), int(size[1])), by, record.get("where"))
    return None


def earlier_bezels(key: str, measured: dict[str, Any] | None, passed: dict[str, list[dict]] | None) -> list[StoredBezel]:
    """Every measure of the insert *key*'s bezel kept before this render: each drawing of the same shot and spec
    that found it (*passed*, :func:`stored_bezel`) and its save's (*measured*, ``insert_bezels.Measured``) - how far off
    it the quad was, and the size each read it at."""
    found = [bezel for record in passed_records(passed, key) if (bezel := stored_bezel([record])) is not None]
    saved = saved_measure(measured, key)
    bezel = getattr(saved, "bezel", None)
    size = getattr(saved, "size", None)
    if bezel is not None and size and not any(item.by.startswith("save") for item in found):
        found.append(StoredBezel(float(bezel.error or 0.0), (int(size[0]), int(size[1])), "the save", saved.where))
    return found


def unmeasured(
    audits: dict[str, dict],
    placed: dict[str, Placed],
    *,
    measured: dict[str, Any] | None = None,
    passed: dict[str, list[dict]] | None = None,
    frame_range: tuple[int, int] | None = None,
) -> dict[str, dict]:
    """The inserts a render drew whole without finding the bezel its save or a drawing of the same shot and spec before
    it found (:func:`earlier_bezels`): ``key -> {earlier, audited, drawn_at}`` - those measures, how many of its
    frames this render audited and at what size. Its measurement is lost (``insert_measurement_lost``, the presence gate
    names its frame and box): what it would say of its presence, its alignment and the frames not drawn is of another
    reading than the one the save and the preview made, never a fact of the video. An insert whose join readings were set
    aside (:func:`bezel_readings`) is measured by the measure that stands."""
    out: dict[str, dict] = {}
    for key, where in placed.items():
        part = audits.get(key) or {}
        if isinstance(part.get("bezel_stored"), dict) or isinstance(part.get("bezel_discarded"), dict):
            continue
        frames = {int(n): audit for n, audit in (part.get("frames") or {}).items()}
        if any(_alignment_of(audit) is not None for audit in frames.values()):
            continue
        placements = placements_of(part)
        if not any(placed_share(placements[n]) >= BLEND_WHOLE for n in where.judged_frames(placements, frame_range) if n in placements):
            continue
        earlier = earlier_bezels(key, measured, passed)
        if earlier:
            out[key] = {"earlier": [bezel.as_dict() for bezel in earlier], "audited": len(frames), "drawn_at": _size_of(frames)}
    return out


@dataclass
class BezelReadings:
    """A render's insert audits with the bezel readings it does not judge set aside (:func:`bezel_readings`): what
    its findings and reports read, the facts saying the measure that stands instead, the lines for the administrator."""

    audits: dict[str, dict]
    facts: list[str] = dataclass_field(default_factory=list)
    infra: list[str] = dataclass_field(default_factory=list)


def _set_aside(part: dict, frames: set[int], **said: dict) -> dict:
    """*part* (an insert's audits) with the bezel finder's reading on *frames* left out, and *said* about it."""
    kept = {
        n: ({k: v for k, v in audit.items() if k not in BEZEL_READING_FIELDS} if int(n) in frames else audit)
        for n, audit in (part.get("frames") or {}).items()
    }
    return {**part, "frames": kept, **said}


def _pixels(size: Any) -> int:
    return int(size[0]) * int(size[1]) if isinstance(size, list | tuple) and len(size) == 2 else 0


def bezel_readings(
    audits: dict[str, dict],
    placed: dict[str, Placed],
    *,
    passed: dict[str, list[dict]] | None = None,
    measured: dict[str, Any] | None = None,
    scene_id: str | None = None,
) -> BezelReadings:
    """The bezel finder's readings on a render's join frames that its findings judge: *audits* with the others
    set aside (:data:`BEZEL_READING_FIELDS`).

    * A neighbour's insert in a scene preview (*scene_id*) is read on its joins only at the size it was last measured
      at - its own scene's drawing as it is now, else its save (*passed*, :func:`stored_bezel`). Drawn at another (a
      720p draft beside a scene its own 1080p preview and its 1080p footage measured), its readings there are set
      aside and that measure is said as a fact (``bezel_stored`` on its report), never a finding.
    * A join frame drawn smaller than the footage frame the save measured a static insert's bezel on (*measured*,
      ``insert_bezels.Measured.size``), whose reading is more than ``surface_refine.ALIGNMENT_MAX_SHARE`` off the
      save's, is the lower-resolution reading: set aside (``bezel_discarded``), the disagreement the platform's, said
      to the administrator (``infra``), never filed against the video. A frame at the save's size or more reads the
      footage whole: it stands.
    """
    out = BezelReadings(audits=dict(audits))
    for key, where in placed.items():
        part = audits.get(key) or {}
        frames = {int(n): audit for n, audit in (part.get("frames") or {}).items()}
        joined = {n for n, audit in frames.items() if where.at_join(n) and _alignment_of(audit) is not None}
        if not joined:
            continue
        size = _size_of({n: frames[n] for n in joined})
        stored = stored_bezel(passed_records(passed, key))
        if scene_id is not None and where.scene_id != scene_id and stored is not None and size and tuple(size) != stored.size:
            out.audits[key] = _set_aside(part, joined, bezel_stored=stored.as_dict())
            out.facts.append(
                f"{where.label}, drawn on this preview's join with scene {where.scene_id} at {size[0]}x{size[1]}: "
                f"{stored.words()} - not measured again at another size: a fact, not a finding."
            )
            continue
        saved = (measured or {}).get(key)
        reference = getattr(getattr(saved, "bezel", None), "error", None)
        read_at = getattr(saved, "size", None)
        if reference is None or read_at is None:
            continue
        lower = {
            n
            for n in joined
            if 0 < _pixels(frames[n].get("size")) < _pixels(read_at) and abs(off_bezel(frames[n]) - reference) > ALIGNMENT_MAX_SHARE
        }
        if not lower:
            continue
        worst = max(lower, key=lambda n: off_bezel(frames[n]))
        drawn = frames[worst]["size"]
        out.audits[key] = _set_aside(part, lower, bezel_discarded={"frames": len(lower), "worst": round(off_bezel(frames[worst]), 5)})
        out.infra.append(
            f"bezel finder, {where.label}: {len(lower)} frame{'s' if len(lower) != 1 else ''} of its scene's joins drawn at "
            f"{int(drawn[0])}x{int(drawn[1])} read its quad up to {off_bezel(frames[worst]) * 100:.2f}% of the frame's width "
            f"off its bezel (mean or one side), where the save measured {reference * 100:.2f}% on {saved.where} at "
            f"{int(read_at[0])}x{int(read_at[1])} - more than {ALIGNMENT_MAX_SHARE * 100:.1f}% apart, so the lower-resolution "
            "reading is set aside for the save's: the platform's measurement, no finding of the video"
        )
    return out


def findings(
    audits: dict[str, dict],
    placed: dict[str, Placed],
    *,
    fps: float,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
    earlier: dict[str, list[dict]] | None = None,
    passed: dict[str, list[dict]] | None = None,
    frame_range: tuple[int, int] | None = None,
    measured: dict[str, Any] | None = None,
    unmeasured: Collection[str] = (),
) -> list[tuple[Finding, str | None]]:
    """The findings of a render's screen inserts, each with the path of the frame that shows it (``None``: none kept).

    *audits* are ``compositor.segment_audits``'; *offset* the MP4's start in
    the plan (a scene preview's window), *span* ``(start, seconds)`` that
    window: a padded insert outside it is not this preview's. *earlier* is
    what the video's kept drawings measured of each insert, newest first
    (``render_checks.earlier_measures``): a finding an earlier
    drawing at another key or light left the same is said so (:func:`unchanged`).
    *passed* is what measured each insert on the same shot and spec before -
    its save (``insert_bezels.verdicts``), a preview of its scene as it is now
    (``render_checks.insert_verdicts``): ``{insert key: [{stage, id, gates}]}``
    - a gate one of them passed is a warning here that disagrees with it,
    never a block (:func:`disagreeing`). *frame_range* is the frames
    drawn (a scene preview's), over which an insert's matte is judged on
    the frames that are its scene's (:func:`owned_flicker`).
    A layer not drawn on a frame the page draws it on is no finding here:
    the presence gate says it where the encoded frame shows it missing
    (:class:`PresenceGate`).
    *measured* is what the save measured of each static insert on its footage
    frame (``insert_bezels.measured_surfaces``): its flatness is that measure's,
    never read again on the frames drawn - one verdict at every resolution
    (:func:`saved_measure`). An insert *unmeasured* (:func:`unmeasured`) has its frames not drawn said by
    its lost measurement, never as a finding of their own.
    """
    from app.services.video_studio import screen_insert

    out: list[tuple[Finding, str | None]] = []
    rate = max(float(fps), 1.0)

    def at(frame: int) -> float:
        return max(0.0, frame / rate - offset)

    for key, where in placed.items():
        part = audits.get(key) or {}
        frames: dict[int, dict] = {int(n): audit for n, audit in (part.get("frames") or {}).items()}
        evidence = part.get("evidence") or {}
        earlier_of_key = (earlier or {}).get(key)
        # On any frame: the insert drawn where the mask every gate reads says something stands in front.
        over = sorted(n for n, audit in frames.items() if (audit.get("covered") or 0.0) > screen_insert.MAX_COVERED_SHARE)
        if over:
            proof = _worst(evidence.get("covered") or [])
            worst_frame = int(proof["frame"]) if proof else max(over, key=lambda n: frames[n]["covered"])
            worst = float(frames.get(worst_frame, {}).get("covered") or 0.0)
            crop = _worst(evidence.get("foreground_crop") or [])
            kept = "; its worst stretch kept at 100%" if crop is not None else ""
            confidence = frames.get(worst_frame, {}).get("front_confidence")
            fix = "give it a key (key: {color}) matching the surface, or a quad on footage where nothing crosses the surface"
            out.append(
                (
                    Finding(
                        INSERT_FOREGROUND,
                        at(over[0]),
                        at(over[-1]),
                        where.scene_id,
                        f"insert over foreground at t={clock(at(worst_frame))}: {where.label} is drawn over what stands "
                        f"in front of its surface on {worst:.1%} of its quad on frame {worst_frame} ({len(over)} "
                        f"frame{'s' if len(over) != 1 else ''} past {screen_insert.MAX_COVERED_SHARE:.1%}{kept}) - {fix}",
                        FAIL,
                        {
                            "insert": key,
                            "frames": len(over),
                            "worst": round(worst, 4),
                            "worst_at": round(at(worst_frame), 3),
                            "worst_frame": worst_frame,
                            "threshold": screen_insert.MAX_COVERED_SHARE,
                            **({"front_confidence": confidence} if confidence is not None else {}),
                        },
                        fix=fix,
                    ),
                    proof.get("path") if proof else None,
                )
            )
        else:
            out.extend(_edge_line_findings(key, where, frames, evidence, at))
        out.extend(_front_findings(key, where, frames, evidence, at))
        out.extend(_matte_findings(key, where, frames, evidence, at))
        hidden = sorted(
            n
            for n, audit in frames.items()
            if audit.get("logo_visible") is not None and audit["logo_visible"] < screen_insert.LOGO_MIN_VISIBLE
        )
        if where.logo and hidden:
            proof = _worst(evidence.get("logo") or [])
            shown = min(float(frames[n]["logo_visible"]) for n in hidden)
            runs = _runs(hidden)
            fix = "keep the surface clear of what passes in front of it, or move the quad where the mark stays in view"
            out.append(
                (
                    Finding(
                        LOGO_LOST,
                        at(hidden[0]),
                        at(hidden[-1]),
                        where.scene_id,
                        f"logo lost at t={clock(at(hidden[0]))}: {where.label} shows only {shown:.0%} of the template's "
                        f"{where.logo} ({len(hidden)} frame{'s' if len(hidden) != 1 else ''} in {len(runs)} "
                        f"stretch{'es' if len(runs) != 1 else ''}) - {fix}",
                        FAIL,
                        {"insert": key, "logo": where.logo, "visible": round(shown, 4), "frames": len(hidden)},
                        fix=fix,
                    ),
                    proof.get("path") if proof else None,
                )
            )
        # The insert's colours as composited against its source: blacks lifted, a hue turned, a cast - read clear
        # of what stands in front, never on a frame it covers most of.
        shifted = sorted(n for n, audit in frames.items() if colour_bad(audit) > 1.0)
        mine: list[tuple[Finding, str | None]] = []
        # On MIN_FAILING_FRAMES frames at least: a frame of noise never shifts an insert's colours.
        if enough(shifted, len(frames)):
            proof = _worst(evidence.get("colour") or [])
            worst_frame = int(proof["frame"]) if proof and int(proof["frame"]) in frames else max(shifted, key=lambda n: colour_bad(frames[n]))
            worst = frames[worst_frame]
            value = max(colour_bad(frames[n]) for n in shifted)
            still = unchanged(where, "colour", value, earlier_of_key)
            chosen = _colour_action(where, worst, still)
            crossing = worst.get("foreground_share")
            covered = f"; what stands in front covers {float(crossing):.0%} of the quad there and is left out" if crossing else ""
            mine.append(
                (
                    Finding(
                        INSERT_COLOUR_SHIFT,
                        at(shifted[0]),
                        at(shifted[-1]),
                        where.scene_id,
                        f"insert colour shift at t={clock(at(worst_frame))}: {where.label} is drawn {', '.join(colour_failures(worst))} "
                        f"against its source ({len(shifted)} frame{'s' if len(shifted) != 1 else ''} past blacks lifted "
                        f"{screen_insert.MAX_BLACK_LIFT:.0%}, a hue turned {screen_insert.MAX_HUE_ROTATION_DEGREES:.0f} degrees or "
                        f"a cast of {screen_insert.MAX_CAST:.0f} grey levels; {colour_numbers(worst)}{covered}) - {chosen['text']}",
                        FAIL,
                        {
                            "insert": key,
                            "frames": len(shifted),
                            "worst_at": round(at(worst_frame), 3),
                            "light": where.light,
                            "foreground": crossing,
                            **{name: worst.get(name) for name in COLOUR_FIELDS},
                            **({"unchanged": still} if still else {}),
                            **_gate_data("colour_shift", worst, value, 1.0, chosen),
                        },
                        fix=chosen["text"],
                    ),
                    proof.get("path") if proof else None,
                )
            )
        mine.extend(
            _compositing_findings(
                key,
                where,
                frames,
                evidence,
                at,
                earlier_of_key,
                owned_flicker(where, frames, placements_of(part)),
                saved=saved_measure(measured, key),
            )
        )
        # A curved surface bows past its flat quad: the key colour there is its own screen's, never a spill onto
        # the room - it follows from the flatness, as its quad off the bezel and the key left past its edge do.
        flat = next((finding for finding, _proof in mine if finding.kind == INSERT_FLATNESS), None)
        if flat is not None:
            spilled_here = [
                item for item in out if item[0].kind == KEY_SPILL_OUTSIDE_QUAD and item[0].data.get("insert") == key
            ]
            if spilled_here:
                out[:] = [item for item in out if item not in spilled_here]
                flat.data["follows"] = [*flat.data.get("follows", []), KEY_SPILL_OUTSIDE_QUAD]
        # What a save's measure or a preview of the same shot and spec passed is no block here.
        out.extend(disagreeing(mine, passed_records(passed, key), fps=rate))
        # A cover fit cutting the source's content, per frame.
        out.extend(_pixel_findings(where, frames, evidence, at))
        # An unkeyed contain fit's bands showing the screen's own key colour.
        out.extend(_band_key_findings(where, frames, at))
        if where.padded:
            if span is not None and not (where.end > span[0] and where.start < span[0] + span[1]):
                continue
            start = where.start - offset
            fix = "use a picture with transparency around the mark, or fit: cover"
            out.append(
                (
                    Finding(
                        INSERT_PADDED,
                        max(0.0, start),
                        max(0.0, where.end - offset),
                        where.scene_id,
                        f"insert padded: {where.label}'s picture holds flat bands {where.padded} inside its quad (a card "
                        f"or a letterbox baked into it) - {fix}",
                        FAIL,
                        {"insert": key, "sides": where.padded},
                        fix=fix,
                    ),
                    None,
                )
            )
    out.extend((finding, None) for finding in soft(placed, offset=offset, span=span))
    return out


def _through_cause(where: Placed, failing: list[dict]) -> str:
    """What an ``insert_through_foreground`` finding says caused it (#10573), from its failing frames' audits: the
    keyed matte already refined on the edge of what stands in front and still failing there - the footage itself,
    no setting clears it -; failing inside what stands in front - the key's tolerance reads the subject part key, which
    a lower ``key.tolerance`` clears -; ``""`` for an unkeyed insert or audits that do not say."""
    if not where.keyed or not failing:
        return ""
    if any(audit.get("matte_refined") for audit in failing) and all(audit.get("through_edge") for audit in failing):
        return (
            "the matte was drawn again refined on the subject's edge and the strokes still show there: it is the "
            "footage - its edge holds the key's colour where the subject should be, which no key setting clears"
        )
    if any(audit.get("through_edge") is False for audit in failing):
        return (
            "the strokes show inside the subject, not on its edge: the key reads part of the subject as the key - "
            "lower key.tolerance until the subject reads clear"
        )
    return ""


def _told_apart(where: Placed) -> str:
    """The one action of a subject the mask cannot tell from the surface: an unkeyed insert's surface keyed, a
    keyed one's subject in colours its key never reads."""
    if where.keyed:
        return (
            "make the shot again with what stands in front in colours far from the key's - none of its edge the key's "
            "colour or as dark as the screen - so the key tells it from the surface"
        )
    return (
        "make the shot again with its screen a flat key colour (a green screen) and give the insert that key "
        "(key: green): the key tells what stands in front from the surface by its colour"
    )


_MATTE_FIX = (
    "key the surface instead (make the shot again with its screen a flat key colour and give the insert key: green), or "
    "make the shot again with the subject clear of the screen's edge, then preview the scene"
)
_OCCLUDED_FIX = (
    "move the subject off the screen (make the shot again with the subject beside it), declare the insert scenery "
    "(legibility: ambient), or make the subject smaller in the shot"
)


#: What a matte finding read only on guessed masks adds to its message (dev:render-picture-check-gate-drift).
_GUESSED_MASK_NOTE = (
    " (an advisory: on these frames the mask of what stands in front is guessed - dark on dark, no key - so the halo "
    "and the lag are as unsure as the mask)"
)


#: A halo's largest patch under this share of the frame is not a patch a viewer sees at full size
#: (dev:render-picture-check-gate-drift-2): on an insert whose mask is unreliable somewhere (:func:`_unreliable_mask`),
#: such a frame never blocks ``insert_matte_halo``, nor ``insert_matte_lag`` while its lag stays under
#: :data:`UNRELIABLE_LAG_FACTOR` times ``screen_insert.MAX_MATTE_LAG_PX``.
MATTE_HALO_VISIBLE_REGION = 0.0001
UNRELIABLE_LAG_FACTOR = 2.0
#: What a matte finding held back that way adds to its message.
_UNRELIABLE_MASK_NOTE = (
    " (an advisory: this insert's mask of what stands in front is guessed - dark on dark, no key, under "
    f"{screen_insert.LOW_FRONT_CONFIDENCE:.0%} sure on some frames - and no patch it leaves reaches "
    f"{MATTE_HALO_VISIBLE_REGION:.2%} of the frame; the lasting fix is a keyed surface: make the shot again with its "
    "screen a flat key colour and give the insert key: green)"
)


def _unreliable_mask(read: dict[int, screen_insert.FrameAudit]) -> bool:
    """Whether an insert's mask of what stands in front is guessed on any of its frames *read* (:func:`_guessed_mask`)."""
    return any(_guessed_mask(audit) for audit in read.values())


def _visible_patch(audit: screen_insert.FrameAudit) -> bool:
    """Whether *audit*'s largest halo patch reaches :data:`MATTE_HALO_VISIBLE_REGION` of the frame."""
    return audit.matte_halo_region is not None and audit.matte_halo_region >= MATTE_HALO_VISIBLE_REGION


def _guessed_mask(audit: screen_insert.FrameAudit) -> bool:
    """Whether *audit*'s mask of what stands in front is reported low-confidence by design (``front_confidence`` under
    ``screen_insert.LOW_FRONT_CONFIDENCE``): told from the surface by its link to the rim, not by its own colours."""
    return audit.front_confidence is not None and audit.front_confidence < screen_insert.LOW_FRONT_CONFIDENCE


def _matte_findings(
    key: str, where: Placed, frames: dict[int, dict], evidence: dict, at: Callable[[int], float]
) -> list[tuple[Finding, str | None]]:
    """An unkeyed insert's mask past its subject and a legible insert hidden by it, each with its worst frame:
    ``insert_matte_halo`` - the surface left showing inside the mask beside the subject (``FrameAudit.matte_haloed``) on
    :data:`MIN_FAILING_FRAMES` frames -; ``insert_matte_lag`` - its centroid past ``screen_insert.MAX_MATTE_LAG_PX`` off
    the subject's on more than :data:`MATTE_LAG_FRAME_SHARE` of its frames -; ``insert_occluded`` - a legible insert
    whose quad what stands in front covers past ``screen_insert.OCCLUDED_SHARE`` on more than
    :data:`OCCLUDED_FRAME_SHARE` of its frames, or past ``screen_insert.OCCLUDED_PEAK`` on any."""
    out: list[tuple[Finding, str | None]] = []
    total = len(frames)
    read = {n: screen_insert.FrameAudit.from_dict(audit) for n, audit in frames.items()}
    # A frame whose mask of what stands in front is guessed (dark on dark, no key: ``front_confidence`` under
    # ``screen_insert.LOW_FRONT_CONFIDENCE``) has a halo and a centroid as unsure as the mask: it never blocks
    # (dev:render-picture-check-gate-drift). Only such frames failing is a warning that says so.
    sure = {n for n, audit in read.items() if not _guessed_mask(audit)}
    # An insert whose mask is unreliable somewhere (dark on dark) blocks only on frames whose patch a viewer sees
    # (dev:render-picture-check-gate-drift-2): an edge share of invisible specks is as unsure as the mask.
    unreliable = _unreliable_mask(read)
    sure_lag = set(sure)
    if unreliable:
        unseen = {n for n in sure if not _visible_patch(read[n])}
        sure -= unseen
        bound = UNRELIABLE_LAG_FACTOR * screen_insert.MAX_MATTE_LAG_PX
        sure_lag -= {n for n in unseen if float(read[n].matte_lag or 0.0) <= bound}
    all_haloed = sorted(n for n, audit in read.items() if audit.matte_haloed)
    sure_haloed = [n for n in all_haloed if n in sure]
    halo_severity = FAIL if enough(sure_haloed, total) else WARN
    haloed = sure_haloed if halo_severity == FAIL else all_haloed
    held_note = _UNRELIABLE_MASK_NOTE if unreliable else _GUESSED_MASK_NOTE
    if enough(haloed, total):
        proof = _worst(evidence.get("matte_halo") or [])
        worst_frame = (
            int(proof["frame"])
            if proof and int(proof["frame"]) in haloed
            else max(haloed, key=lambda n: float(read[n].matte_halo or 0.0))
        )
        worst = float(read[worst_frame].matte_halo or 0.0)
        region = float(read[worst_frame].matte_halo_region or 0.0)
        kept = "; its worst stretch kept at 100%" if _worst(evidence.get("matte_halo_crop") or []) is not None else ""
        chosen = action(REGENERATE_SHOT, _MATTE_FIX)
        run = longest_run(haloed) or (haloed[0], haloed[0])
        out.append(
            (
                Finding(
                    INSERT_MATTE_HALO,
                    at(haloed[0]),
                    at(haloed[-1]),
                    where.scene_id,
                    f"insert matte halo at t={clock(at(worst_frame))}: {where.label}'s mask of what stands in front holds "
                    f"the bare surface beside the subject along {worst:.0%} of its edge on frame {worst_frame} (its largest "
                    f"patch {region:.3%} of the frame; {len(haloed)} of {total} frames past "
                    f"{screen_insert.MAX_MATTE_HALO_SHARE:.0%} of the edge or {screen_insert.MAX_MATTE_HALO_REGION:.2%} of the "
                    f"frame{kept}): the footage shows there instead of the insert, a dark halo or a ghost of the subject "
                    f"- {chosen['text']}" + (held_note if halo_severity == WARN else ""),
                    halo_severity,
                    {
                        "insert": key,
                        "frames": len(haloed),
                        **({"guessed_mask": True} if halo_severity == WARN else {}),
                        **({"unreliable_mask": True} if halo_severity == WARN and unreliable else {}),
                        "of": total,
                        "worst": round(worst, 4),
                        "region": round(region, 6),
                        "run": [run[0], run[1]],
                        "worst_at": round(at(worst_frame), 3),
                        "worst_frame": worst_frame,
                        "threshold": screen_insert.MAX_MATTE_HALO_SHARE,
                        "action": chosen,
                    },
                    fix=chosen["text"],
                ),
                proof.get("path") if proof else None,
            )
        )
    all_lagging = sorted(n for n, audit in read.items() if audit.matte_lags)
    sure_lagging = [n for n in all_lagging if n in sure_lag]
    lag_blocks = len(sure_lagging) > MATTE_LAG_FRAME_SHARE * total
    lagging = sure_lagging if lag_blocks else all_lagging
    if lagging and len(lagging) > MATTE_LAG_FRAME_SHARE * total:
        worst_frame = max(lagging, key=lambda n: float(read[n].matte_lag or 0.0))
        worst = float(read[worst_frame].matte_lag or 0.0)
        chosen = action(REGENERATE_SHOT, _MATTE_FIX)
        out.append(
            (
                Finding(
                    INSERT_MATTE_LAG,
                    at(lagging[0]),
                    at(lagging[-1]),
                    where.scene_id,
                    f"insert matte lag at t={clock(at(worst_frame))}: {where.label}'s mask of what stands in front sits "
                    f"{worst:.1f} px off the subject on frame {worst_frame} ({len(lagging)} of {total} frames past "
                    f"{screen_insert.MAX_MATTE_LAG_PX:.0f} px at 1080p, more than {MATTE_LAG_FRAME_SHARE:.0%} of them): the "
                    f"insert is cut where the subject was and drawn where it is - {chosen['text']}"
                    + ("" if lag_blocks else held_note),
                    FAIL if lag_blocks else WARN,
                    {
                        "insert": key,
                        "frames": len(lagging),
                        **({} if lag_blocks else {"guessed_mask": True}),
                        **({"unreliable_mask": True} if not lag_blocks and unreliable else {}),
                        "of": total,
                        "worst": round(worst, 2),
                        "worst_at": round(at(worst_frame), 3),
                        "worst_frame": worst_frame,
                        "threshold": screen_insert.MAX_MATTE_LAG_PX,
                        "action": chosen,
                    },
                    fix=chosen["text"],
                ),
                None,
            )
        )
    if where.legible:
        shares = {n: float(audit.foreground_share) for n, audit in read.items() if audit.foreground_share is not None}
        over = sorted(n for n, share in shares.items() if share > screen_insert.OCCLUDED_SHARE)
        peak = max(shares.values(), default=0.0)
        if over and (len(over) > OCCLUDED_FRAME_SHARE * total or peak > screen_insert.OCCLUDED_PEAK):
            worst_frame = max(over, key=lambda n: shares[n])
            proof = _worst(evidence.get("covered") or [])
            out.append(
                (
                    Finding(
                        INSERT_OCCLUDED,
                        at(over[0]),
                        at(over[-1]),
                        where.scene_id,
                        f"insert occluded at t={clock(at(worst_frame))}: what stands in front hides {peak:.0%} of "
                        f"{where.label}'s quad on frame {worst_frame} ({len(over)} of {total} frames past "
                        f"{screen_insert.OCCLUDED_SHARE:.0%}, more than {OCCLUDED_FRAME_SHARE:.0%} of them or past "
                        f"{screen_insert.OCCLUDED_PEAK:.0%} on one): the insert is there to be read - {_OCCLUDED_FIX}",
                        FAIL,
                        {
                            "insert": key,
                            "frames": len(over),
                            "of": total,
                            "worst": round(peak, 4),
                            "worst_at": round(at(worst_frame), 3),
                            "worst_frame": worst_frame,
                            "threshold": screen_insert.OCCLUDED_SHARE,
                            "peak": screen_insert.OCCLUDED_PEAK,
                        },
                        fix=_OCCLUDED_FIX,
                    ),
                    proof.get("path") if proof else None,
                )
            )
    return out


def _edge_line_findings(
    key: str, where: Placed, frames: dict[int, dict], evidence: dict, at: Callable[[int], float]
) -> list[tuple[Finding, str | None]]:
    """``insert_foreground`` for *key*'s quad edge drawn as a thin line over what stands in front (#11460): on any frame
    past ``screen_insert.MAX_EDGE_LINE_SHARE`` (``FrameAudit.edge_line``), its worst frame and its crop at 100 %."""
    lined = sorted(
        n for n, audit in frames.items() if (audit.get("edge_line") or 0.0) > screen_insert.MAX_EDGE_LINE_SHARE
    )
    if not lined:
        return []
    proof = _worst(evidence.get("edge_line") or [])
    worst_frame = (
        int(proof["frame"])
        if proof and int(proof["frame"]) in frames
        else max(lined, key=lambda n: float(frames[n]["edge_line"]))
    )
    worst = float(frames[worst_frame].get("edge_line") or 0.0)
    kept = "; its worst stretch kept at 100%" if _worst(evidence.get("foreground_crop") or []) is not None else ""
    fix = "move the quad onto the surface's bezel, or a shot where nothing crosses the surface's edge"
    return [
        (
            Finding(
                INSERT_FOREGROUND,
                at(lined[0]),
                at(lined[-1]),
                where.scene_id,
                f"insert edge over foreground at t={clock(at(worst_frame))}: {where.label} draws its quad's edge as a "
                f"line over what stands in front of its surface ({worst:.3f} px per px of its outline on frame "
                f"{worst_frame}, {len(lined)} frame{'s' if len(lined) != 1 else ''} past "
                f"{screen_insert.MAX_EDGE_LINE_SHARE}{kept}) - {fix}",
                FAIL,
                {
                    "insert": key,
                    "edge_line": True,
                    "frames": len(lined),
                    "worst": round(worst, 5),
                    "worst_at": round(at(worst_frame), 3),
                    "worst_frame": worst_frame,
                    "threshold": screen_insert.MAX_EDGE_LINE_SHARE,
                },
                fix=fix,
            ),
            proof.get("path") if proof else None,
        )
    ]


def _front_findings(
    key: str, where: Placed, frames: dict[int, dict], evidence: dict, at: Callable[[int], float]
) -> list[tuple[Finding, str | None]]:
    """What stands in front of *key*'s surface as the audience sees it, each finding with its worst frame and one
    action: ``insert_through_foreground`` - the source's own strokes drawn across its core on more than
    ``screen_insert.MAX_THROUGH_SHARE`` of it, on any frame, with its worst stretch at 100 % -; ``insert_ghost`` - its
    mask under ``screen_insert.LOW_FRONT_CONFIDENCE`` while it covers more than ``screen_insert.GHOST_MIN_FOREGROUND``
    of the quad (``FrameAudit.ghosted``) on more than :data:`GHOST_FRAME_SHARE` of the insert's frames -; and
    ``key_spill_outside_quad`` - the key's hue past the quad on more than ``screen_insert.SPILL_MIN_SHARE`` of the
    frame, on any frame, the whole frame kept."""
    out: list[tuple[Finding, str | None]] = []
    through = sorted(n for n, audit in frames.items() if (audit.get("through") or 0.0) > screen_insert.MAX_THROUGH_SHARE)
    if through:
        proof = _worst(evidence.get("through") or [])
        worst_frame = int(proof["frame"]) if proof and int(proof["frame"]) in frames else max(through, key=lambda n: float(frames[n]["through"]))
        worst = float(frames[worst_frame].get("through") or 0.0)
        kept = "; its worst stretch kept at 100%" if _worst(evidence.get("through_crop") or []) is not None else ""
        chosen = action(REGENERATE_SHOT, _told_apart(where))
        cause = _through_cause(where, [frames[n] for n in through])
        if cause:
            chosen = {**chosen, "text": f"{cause}; {chosen['text']}"}
        through_run = longest_run(through) or (through[0], through[0])
        out.append(
            (
                Finding(
                    INSERT_THROUGH_FOREGROUND,
                    at(through[0]),
                    at(through[-1]),
                    where.scene_id,
                    f"insert seen through the foreground at t={clock(at(worst_frame))}: {where.label} draws its source's own "
                    f"strokes across what stands in front of its surface on {worst:.2%} of it on frame {worst_frame} "
                    f"({len(through)} frame{'s' if len(through) != 1 else ''} past {screen_insert.MAX_THROUGH_SHARE:.1%}{kept}) "
                    f"- {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(through),
                        "worst": round(worst, 5),
                        "of": len(frames),
                        "run": [through_run[0], through_run[1]],
                        "worst_at": round(at(worst_frame), 3),
                        "worst_frame": worst_frame,
                        "threshold": screen_insert.MAX_THROUGH_SHARE,
                        "action": chosen,
                    },
                    fix=chosen["text"],
                ),
                proof.get("path") if proof else None,
            )
        )
    ghosts = sorted(n for n, audit in frames.items() if screen_insert.FrameAudit.from_dict(audit).ghosted)
    run = longest_run(ghosts)
    run_long = run is not None and run[1] - run[0] + 1 >= GHOST_RUN_FRAMES
    if ghosts and (len(ghosts) > GHOST_FRAME_SHARE * len(frames) or run_long):
        proof = _worst(evidence.get("ghost") or [])
        worst_frame = (
            int(proof["frame"]) if proof and int(proof["frame"]) in ghosts else min(ghosts, key=lambda n: float(frames[n]["front_confidence"]))
        )
        run = run or (ghosts[0], ghosts[0])
        run_words = (
            f"; its longest run frames {run[0]}-{run[1]} ({run[1] - run[0] + 1} in a row, t={clock(at(run[0]))}-"
            f"{clock(at(run[1]))})"
        )
        lowest = float(frames[worst_frame]["front_confidence"])
        covers = float(frames[worst_frame].get("foreground_share") or 0.0)
        chosen = action(REGENERATE_SHOT, _told_apart(where))
        out.append(
            (
                Finding(
                    INSERT_GHOST,
                    at(ghosts[0]),
                    at(ghosts[-1]),
                    where.scene_id,
                    f"insert ghost at t={clock(at(worst_frame))}: {where.label}'s mask of what stands in front of its surface "
                    f"is {lowest:.0%} sure on frame {worst_frame}, where it covers {covers:.0%} of the quad ({len(ghosts)} of "
                    f"{len(frames)} frames under {screen_insert.LOW_FRONT_CONFIDENCE:.0%} sure past "
                    f"{screen_insert.GHOST_MIN_FOREGROUND:.0%} of the quad, more than {GHOST_FRAME_SHARE:.0%} of them or "
                    f"{GHOST_RUN_FRAMES} in a row{run_words}): told from "
                    "the surface by its link to what crosses the surface's edge, not by its own colour, the insert shows "
                    f"through it wherever that link falls short - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(ghosts),
                        "of": len(frames),
                        "lowest": round(lowest, 4),
                        "foreground": round(covers, 4),
                        "run": [run[0], run[1]],
                        "run_at": [round(at(run[0]), 3), round(at(run[1]), 3)],
                        "worst_at": round(at(worst_frame), 3),
                        "worst_frame": worst_frame,
                        "threshold": screen_insert.LOW_FRONT_CONFIDENCE,
                        "action": chosen,
                    },
                    fix=chosen["text"],
                ),
                proof.get("path") if proof else None,
            )
        )
    spilled = sorted(n for n, audit in frames.items() if (audit.get("key_spill") or 0.0) > screen_insert.SPILL_MIN_SHARE)
    if spilled:
        proof = _worst(evidence.get("spill") or [])
        worst_frame = int(proof["frame"]) if proof and int(proof["frame"]) in frames else max(spilled, key=lambda n: float(frames[n]["key_spill"]))
        worst = float(frames[worst_frame].get("key_spill") or 0.0)
        box = frames[worst_frame].get("key_spill_box")
        around = f" around {_box_words(box)}" if isinstance(box, list | tuple) and len(box) == 4 else ""
        kept = "; the whole frame kept" if proof else ""
        side = frames[worst_frame].get("key_spill_side")
        reaches = frames[worst_frame].get("key_spill_to")
        if side in ("top", "right", "bottom", "left") and reaches is not None:
            # The key running on from the quad's edge: the screen continuing beside or behind what stands in front,
            # never the footage's spill - no retake, no despill clears it (dev:screen-insert-occluded-edge-quad).
            axis = "y" if side in ("top", "bottom") else "x"
            chosen = action(
                GIVE_QUAD,
                f"widen the quad's {side} side to {axis} {float(reaches):.3f}: the screen continues there (its key "
                "colour runs on from that side, beside or behind what stands in front)",
            )
        else:
            chosen = action(
                REGENERATE_SHOT,
                "make the shot again with nothing past the screen in its key colour - a matte floor and walls it does "
                "not light, no reflection of the screen - so only the screen carries the key",
            )
        out.append(
            (
                Finding(
                    KEY_SPILL_OUTSIDE_QUAD,
                    at(spilled[0]),
                    at(spilled[-1]),
                    where.scene_id,
                    f"key spill outside the quad at t={clock(at(worst_frame))}: {where.label}'s key colour shows past its "
                    f"quad on {worst:.1%} of the frame on frame {worst_frame}{around} ({len(spilled)} frame"
                    f"{'s' if len(spilled) != 1 else ''} past {screen_insert.SPILL_MIN_SHARE:.1%} of the frame{kept}) - "
                    f"{chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(spilled),
                        "worst": round(worst, 5),
                        "box": list(box) if isinstance(box, list | tuple) else None,
                        "worst_at": round(at(worst_frame), 3),
                        "worst_frame": worst_frame,
                        "threshold": screen_insert.SPILL_MIN_SHARE,
                        "side": side if chosen["kind"] == GIVE_QUAD else None,
                        "widen_to": round(float(reaches), 4)
                        if chosen["kind"] == GIVE_QUAD and reaches is not None
                        else None,
                        "action": chosen,
                    },
                    fix=chosen["text"],
                ),
                proof.get("path") if proof else None,
            )
        )
    return out


def _box_words(box: Any) -> str:
    """A box of the frame (0-1: ``[x0, y0, x1, y1]``) as a finding says it: ``x 0.30-0.70, y 0.80-0.98 of the frame``."""
    x0, y0, x1, y1 = (float(v) for v in box)
    return f"x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f} of the frame"


def saved_measure(measured: dict[str, Any] | None, key: str) -> Any | None:
    """What the save measured of the insert *key* (*measured*, :func:`findings`): its own, else its overlay's when *key*
    is one of its panes (``insert/<scene>.<n>.<pane>``); ``None`` when it measured none."""
    if not measured:
        return None
    parts = key.split(".")
    return measured.get(key) or (measured.get(".".join(parts[:2])) if len(parts) > 2 else None)


def passed_records(passed: dict[str, list[dict]] | None, key: str) -> list[dict]:
    """What measured the insert *key* before on the same shot and spec (*passed*, :func:`findings`): its own records,
    and its overlay's when the records name the overlay and *key* is one of its panes (``insert/<scene>.<n>.<pane>``)."""
    if not passed:
        return []
    found = list(passed.get(key) or [])
    parts = key.split(".")
    if len(parts) > 2:
        found.extend(passed.get(".".join(parts[:2])) or [])
    return [record for record in found if isinstance(record, dict)]


def disagreement_cause(record: dict, fps: float | None) -> str:
    """Why a drawing at *fps* reads a gate otherwise than the earlier *record* (:func:`disagreeing`), in words the
    author decides on: the save measured one footage frame; a drawing at another frame rate judged other frames, which a
    new preview at this rate reads too; one at the same rate drew the same instants, so the frames themselves differ
    (the encode, the corners followed again) and a new preview may not reproduce it."""
    stage = str(record.get("stage") or "drawing")
    if stage == "save":
        return "the save measured the footage frame the insert comes in on alone, this drawing every frame it draws"
    then = record.get("fps")
    if not then or not fps:
        return f"the {stage}'s frame rate is not recorded: preview the scene again to compare the same frames"
    then, now = float(then), float(fps)
    if abs(then - now) > 0.01:
        return (
            f"the {stage} judged it at {then:g} fps and this drawing at {now:g} fps: frames the {stage} never drew are "
            f"read here, and a preview at {now:g} fps reads them too"
        )
    return (
        f"both judged it at {now:g} fps, the same instants: the frames drawn differ (the encode, or the corners "
        "followed again), so a new preview may not reproduce it"
    )


def disagreeing(
    found: list[tuple[Finding, str | None]], records: list[dict], *, fps: float | None = None
) -> list[tuple[Finding, str | None]]:
    """*found* with each failing finding of a gate one of *records* passed on the same shot and spec made a warning:
    ``disagrees with save revision 7: ...`` - the two stages measure one thing, so their disagreement is
    said, never what blocks (``data["disagrees"]``), with its cause (:func:`disagreement_cause`, the drawing's *fps*
    against the record's). What blocks on a frame of a join or a frame the insert was not
    drawn on, or on a bezel that moved from under the lock (``data["drifted"]``), stays a block: no
    earlier drawing measured those frames as the render draws them - the save read the first frame alone."""
    import dataclasses

    out = []
    for finding, proof in found:
        if finding.data.get("transition") or finding.data.get("absent") or finding.data.get("drifted"):
            # A bezel moving from under the lock is read on frames the save, on the first one, never measured.
            out.append((finding, proof))
            continue
        gate = GATE_OF.get(finding.kind)
        record = next((r for r in records if (r.get("gates") or {}).get(gate) is True), None) if gate else None
        if record is None or finding.severity != FAIL:
            out.append((finding, proof))
            continue
        named = f"{record.get('stage') or 'drawing'} {record.get('id') or ''}".strip()
        cause = disagreement_cause(record, fps)
        data = {**finding.data, "disagrees": {"stage": record.get("stage"), "id": record.get("id"), "gate": gate, "cause": cause}}
        message = f"disagrees with {named}: {finding.message}; they disagree because {cause}"
        out.append((dataclasses.replace(finding, severity=WARN, message=message, data=data, code=None, params=None), proof))
    return out


def ratio_for(css_width: float, found: screen_insert.Density) -> float | None:
    """The smallest pixel ratio a recording is made at whose frames, *css_width* window pixels wide, pass the check on
    the quad *found* was measured on (its ``least``, the same proportions); ``None`` when none does."""
    from app.services.video_studio.insert_legibility import RECORD_RATIOS

    return next((ratio for ratio in RECORD_RATIOS if css_width * ratio >= found.least[0]), None)


def _needed_text(source: tuple[str, str] | None, soft_record: dict) -> str:
    """What the source needs to be sharp: the smallest size the check passes and its size at one pixel per
    pixel - for a recording, the pixel ratio too."""
    needed = soft_record.get("needed") or [0, 0]
    least = soft_record.get("least")
    text = (
        f"a source at least {int(least[0])}x{int(least[1])} px ({int(needed[0])}x{int(needed[1])} draws it one to one)"
        if least
        else f"a source at least {int(needed[0])}x{int(needed[1])} px"
    )
    kind = source[0] if source else None
    if kind == "take" and soft_record.get("pixel_ratio"):
        text += f" - record it again with pixel_ratio: {soft_record['pixel_ratio']:g}"
    elif kind == "take":
        text += " - record it again at a higher pixel_ratio or a larger viewport"
    elif kind == "clip":
        text += " - generate the shot at a higher resolution, or give the insert a smaller quad"
    return text


def soft(placed: dict[str, Placed], *, offset: float = 0.0, span: tuple[float, float] | None = None) -> list[Finding]:
    """The ``INSERT_SOFT`` findings of the inserts *placed* whose source is too small for its quad at the render's size.

    The density travels on the insert's payload (``soft``, ``render._matte_inserts``: source pixels per quad pixel
    at the render's size, ``screen_insert.density``); one under ``screen_insert.MIN_DENSITY`` names the source and the
    size it needs. Timed at the insert, in the MP4's time; outside a preview's window none.
    """
    out: list[Finding] = []
    for where in placed.values():
        record = where.soft or {}
        value = record.get("value")
        if value is None or float(value) >= screen_insert.MIN_DENSITY:
            continue
        if span is not None and not (where.end > span[0] and where.start < span[0] + span[1]):
            continue
        source_size, quad = record.get("source") or [0, 0], record.get("quad") or [0, 0]
        named = f"{where.source[0]} `{where.source[1]}`" if where.source else "its source"
        fix = f"it needs {_needed_text(where.source, record)}"
        out.append(
            Finding(
                INSERT_SOFT,
                max(0.0, where.start - offset),
                max(0.0, where.end - offset),
                where.scene_id,
                f"insert soft: {where.label} draws {named} ({int(source_size[0])}x{int(source_size[1])} px) on a quad "
                f"{int(quad[0])}x{int(quad[1])} px at the render's size: {float(value):.2f} source pixels per pixel "
                f"(at least {screen_insert.MIN_DENSITY:g}) - {fix}",
                FAIL,
                {"insert": where.key, **record, **({"source_kind": where.source[0], "source_id": where.source[1]} if where.source else {})},
                fix=fix,
            )
        )
    return out


def _points_on_frame(quad: Any, footage: tuple[int, int] | None, size: tuple[int, int]) -> tuple[tuple[float, float], ...]:
    """*quad*'s corners (0-1 across its footage) in render pixels of a frame of *size* the footage, of *footage* size
    (the frame's when unknown), covers, from the footage's top left corner."""
    width, height = size
    src_w, src_h = footage or (width, height)
    cover = max(width / max(src_w, 1), height / max(src_h, 1))
    return tuple((float(x) * src_w * cover, float(y) * src_h * cover) for x, y in quad)


def quad_on_frame(quad: Any, footage: tuple[int, int] | None, size: tuple[int, int]) -> tuple[float, float]:
    """``(width, height)`` in render pixels of *quad* (0-1 across its footage) on a frame of *size*: the footage, of
    *footage* size (the frame's when unknown), covers the frame."""
    return quad_size(_points_on_frame(quad, footage, size))


def box_on_frame(quad: Any, footage: tuple[int, int] | None, size: tuple[int, int]) -> tuple[float, float, float, float]:
    """``(left, top, right, bottom)`` of *quad* on a frame of *size*, as :func:`quad_on_frame` places it: where two
    surfaces stand to each other."""
    points = _points_on_frame(quad, footage, size)
    xs, ys = [x for x, _y in points], [y for _x, y in points]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class Surface:
    """A screen insert surface as a render of one size draws it: its quad in the render's pixels, the insert's
    own push (``zoom.scale``) and its fit - what :func:`screen_insert.density` judges its source on."""

    key: str
    quad: tuple[float, float]
    push: float
    fit: str

    @property
    def drawn(self) -> tuple[float, float]:
        """Its quad at the peak of its push: the source drawn as on a quad that many times larger."""
        return self.quad[0] * self.push, self.quad[1] * self.push

    def density(self, source_size: tuple[int, int]) -> screen_insert.Density | None:
        return screen_insert.density(source_size, *self.drawn, self.fit)


def _surface(key: str, part: Any, footage: tuple[int, int] | None, size: tuple[int, int]) -> Surface:
    push = float(part.overlay.zoom.scale) if part.overlay.zoom is not None else 1.0
    return Surface(key, quad_on_frame(part.quad, footage, size), push, part.overlay.fit or "cover")


@dataclass(frozen=True)
class Soft:
    """A screen insert surface or a replaced screen too soft for a render of one size (:func:`soft_before`)."""

    scene_id: str
    #: The screen insert surface (``timeline.insert_key``) and its index among its scene's overlays; ``None`` for a
    #: replaced screen.
    key: str | None
    index: int | None
    #: What it shows, ``(kind, id)``: a replaced screen's is its shot.
    source: tuple[str, str]
    #: What the gate says of it, its scene left out.
    words: str
    #: A screen insert surface's reading at that size: the surface and its source's density on it (``None``: a
    #: replaced screen).
    surface: Surface | None = None
    density: screen_insert.Density | None = None

    @property
    def message(self) -> str:
        return f"scene {self.scene_id}: {self.words}"

    @property
    def where(self) -> str:
        return self.key or f"screen/{self.scene_id}"

    @property
    def pane(self) -> int | None:
        """Its pane among its wall's (``timeline.insert_key``); ``None``: no panes, or a replaced screen."""
        parts = (self.key or "").removeprefix("insert/").split(".")
        return int(parts[2]) if len(parts) > 2 else None


def _soft_surfaces(
    spec: Any,
    size: tuple[int, int],
    *,
    sources: dict[tuple[str, str], tuple[int, int]],
    footage: dict[str, tuple[int, int]],
    pixel_ratios: dict[str, float],
    screens_replaced: dict[str, dict],
) -> list[Soft]:
    """The :class:`Soft` surfaces of *spec* at *size*: :func:`soft_before`'s reading, its arguments."""
    width, height = size
    out: list[Soft] = []

    from app.services.video_studio import timeline

    for key, part in timeline.insert_sources(spec).items():
        scene_id = timeline.insert_scene(key)
        source_size = sources.get((part.kind, part.ref))
        if source_size is None:
            continue
        surface = _surface(key, part, footage.get(scene_id), size)
        found = surface.density(source_size)
        if found is None or not found.soft:
            continue
        record = found.as_dict()
        ratio = pixel_ratios.get(part.ref) if part.kind == "take" else None
        if ratio and (wanted := ratio_for(max(source_size[0] / ratio, 1.0), found)) is not None:
            record["pixel_ratio"] = wanted
        pushed = f", pushed in {surface.push:g}x by its zoom" if surface.push > 1 else ""
        quad_w, quad_h = surface.quad
        out.append(
            Soft(
                scene_id,
                key,
                part.index,
                (part.kind, part.ref),
                f"{timeline.insert_label(key)} draws {part.kind} `{part.ref}` ({source_size[0]}x{source_size[1]} px) on "
                f"a quad {round(quad_w)}x{round(quad_h)} px at {width}x{height}{pushed}: {found.value:.2f} source pixels "
                f"per pixel (at least {screen_insert.MIN_DENSITY:g}: a soft insert) - it needs "
                f"{_needed_text((part.kind, part.ref), record)}",
                surface,
                found,
            )
        )
    for scene in spec.scenes:
        shot = scene.clip_id if scene.type == "clip" else None
        record = screens_replaced.get(shot or "") if shot else None
        if not isinstance(record, dict) or not record.get("picture_size") or not record.get("quad") or not record.get("start_size"):
            continue
        start_w, start_h = (max(float(v), 1.0) for v in record["start_size"])
        src_w, src_h = footage.get(scene.id or "") or (start_w, start_h)
        cover = max(width / max(src_w, 1), height / max(src_h, 1))
        scale_x, scale_y = cover * src_w / start_w, cover * src_h / start_h
        quad_w, quad_h = quad_size(tuple((float(x) * scale_x, float(y) * scale_y) for x, y in record["quad"]))
        picture_w, picture_h = (int(v) for v in record["picture_size"])
        found = screen_insert.density((picture_w, picture_h), quad_w, quad_h, "stretch")
        if found is None or not found.soft:
            continue
        out.append(
            Soft(
                scene.id,
                None,
                None,
                ("clip", str(shot)),
                f"its replaced screen draws a {picture_w}x{picture_h} px screenshot on a screen {found.quad[0]}x"
                f"{found.quad[1]} px at {width}x{height}: {found.value:.2f} source pixels per pixel (at least "
                f"{screen_insert.MIN_DENSITY:g}: a soft insert) - give the shot `{shot}` a screenshot at least "
                f"{found.needed[0]}x{found.needed[1]} px, or render at a lower quality",
            )
        )
    return out


def soft_before(
    spec: Any,
    size: tuple[int, int],
    *,
    sources: dict[tuple[str, str], tuple[int, int]],
    footage: dict[str, tuple[int, int]],
    pixel_ratios: dict[str, float],
    screens_replaced: dict[str, dict],
) -> list[tuple[str, str]]:
    """``(scene id, why)`` of the inserts and replaced screens of *spec* too soft for a render of *size*, before it runs.

    The gate's reading (``render_checks.gate``): *sources* are the sizes of
    the inserts' sources by ``(kind, id)`` (a recording's frames, a shot's,
    a picture's; a source of unknown size is not judged), *footage* each
    scene's footage size by scene id (the frame when unknown: the footage
    covers it), *pixel_ratios* a recording's by id, *screens_replaced* a
    shot's ``params.screen`` by shot id. The same measure the render makes
    (``screen_insert.density``), at the peak of the insert's own push
    (``zoom``: the source drawn as on a quad that many times larger), without
    the camera's zoom (the render's own finding has it): a recording the gate
    passes may still be soft under a zoom, never the other way round.
    """
    return [
        (item.scene_id, item.message)
        for item in _soft_surfaces(
            spec, size, sources=sources, footage=footage, pixel_ratios=pixel_ratios, screens_replaced=screens_replaced
        )
    ]


def _listed(names: tuple[str, ...]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass(frozen=True)
class SoftAt:
    """A surface soft at some of the qualities a whole video may be rendered at (:func:`soft_by_quality`)."""

    #: Its reading at the largest of them: a source sharp there is sharp at every quality.
    soft: Soft
    #: The qualities it is soft at, and those it is sharp at, smallest first.
    qualities: tuple[str, ...]
    sharp: tuple[str, ...]

    @property
    def scene_id(self) -> str:
        return self.soft.scene_id

    @property
    def identity(self) -> str:
        """The surface, what it shows and where it is soft: what a save's answer says once (``insert_legibility.said_once``)."""
        return f"{self.soft.where}|{self.soft.source[0]}:{self.soft.source[1]}|{','.join(self.qualities)}"

    def message(self) -> str:
        """``soft at ultra, which render_video refuses: <the gate's words>; standard and high draw it sharp``, a
        recording told the call that sizes it for its quad."""
        largest = self.qualities[-1]
        sized = (
            f'; record_browser_demo(target_render: "{largest}", video_id, scene: "{self.scene_id}", insert: '
            f"{self.soft.index}) sizes the recording for this quad"
            if self.soft.source[0] == "take" and self.soft.index is not None
            else ""
        )
        sharp = f"; {_listed(self.sharp)} draw{'s' if len(self.sharp) == 1 else ''} it sharp" if self.sharp else ""
        return f"soft at {_listed(self.qualities)}, which render_video refuses: {self.soft.words}{sized}{sharp}"


def soft_by_quality(
    spec: Any,
    *,
    sources: dict[tuple[str, str], tuple[int, int]],
    footage: dict[str, tuple[int, int]],
    pixel_ratios: dict[str, float],
    screens_replaced: dict[str, dict],
    qualities: tuple[str, ...] | None = None,
) -> list[SoftAt]:
    """Every surface of *spec* the gate (:func:`soft_before`, its arguments) would refuse at one of *qualities* - unset,
    every quality a whole video may be rendered at (``spec.RENDER_QUALITIES``) -, before any render.

    A preview is drawn at 1080p: an insert soft at 4K alone passed every
    preview and was refused by ``render_video`` once every scene was green,
    its sources recorded again and every scene previewed again. Each surface
    is said once, with the qualities it is soft at and those it is sharp at,
    read at the largest - a source the size it names there is sharp at every
    quality.
    """
    from app.services.video_studio.spec import QUALITY_PRESETS, RENDER_QUALITIES, output_size

    ordered = sorted(dict.fromkeys(qualities or RENDER_QUALITIES), key=lambda name: QUALITY_PRESETS[name].short_side)
    found: dict[str, tuple[Soft, list[str]]] = {}
    for quality in ordered:
        size = output_size(spec.format.aspect_ratio, QUALITY_PRESETS[quality].short_side)
        for item in _soft_surfaces(
            spec, size, sources=sources, footage=footage, pixel_ratios=pixel_ratios, screens_replaced=screens_replaced
        ):
            soft_at = found[item.where][1] if item.where in found else []
            found[item.where] = (item, [*soft_at, quality])
    return [
        SoftAt(item, tuple(soft_at), tuple(name for name in ordered if name not in soft_at))
        for item, soft_at in found.values()
    ]


def surfaces_at(
    spec: Any, size: tuple[int, int], footage: dict[str, tuple[int, int]], *, scene_id: str, index: int | None = None
) -> list[Surface]:
    """The screen insert surfaces of *scene_id* - of its screen insert *index* alone, a wall pane by pane - as a render
    of *size* draws them; none for a quad still left to the render."""
    from app.services.video_studio import timeline

    return [
        _surface(key, part, footage.get(scene_id), size)
        for key, part in timeline.insert_sources(spec).items()
        if timeline.insert_scene(key) == scene_id and (index is None or part.index == index)
    ]


@dataclass(frozen=True)
class Recording:
    """A recording sized for the surfaces it is drawn on at one render size (:func:`recording_for`)."""

    viewport: tuple[int, int]
    pixel_ratio: float
    #: The browser zoom it is recorded at, in percent (``spec.TakeScript.browser_zoom``).
    browser_zoom: int
    #: Its frames' size, and its density on the surface that asks the most: at least ``screen_insert.MIN_DENSITY``.
    frame: tuple[int, int]
    density: screen_insert.Density

    @property
    def css_width(self) -> int:
        """The width its page is laid out at: its window over its zoom (``spec.zoomed_window``)."""
        return round(self.viewport[0] * 100 / self.browser_zoom)


def quad_window(surfaces: list[Surface], viewport: tuple[int, int]) -> tuple[int, int]:
    """The window a recording drawn on *surfaces* is made at: *viewport*'s width, its height from the proportions
    of the largest surface's quad - its frames fill the quad, a contain fit leaves no band -, held within the windows a
    recording takes (``spec``'s, its width given way where its height reaches a bound)."""
    from app.services.video_studio.spec import (
        MAX_VIEWPORT_HEIGHT,
        MAX_VIEWPORT_WIDTH,
        MIN_VIEWPORT_HEIGHT,
        MIN_VIEWPORT_WIDTH,
    )

    quad_w, quad_h = max(surfaces, key=lambda surface: surface.quad[0] * surface.quad[1]).quad if surfaces else (0.0, 0.0)
    if quad_w <= 0 or quad_h <= 0:
        return viewport
    aspect = quad_w / quad_h
    width = min(max(int(viewport[0]), MIN_VIEWPORT_WIDTH), MAX_VIEWPORT_WIDTH)
    height = round(width / aspect)
    if not MIN_VIEWPORT_HEIGHT <= height <= MAX_VIEWPORT_HEIGHT:
        height = min(max(height, MIN_VIEWPORT_HEIGHT), MAX_VIEWPORT_HEIGHT)
        width = min(max(round(height * aspect), MIN_VIEWPORT_WIDTH), MAX_VIEWPORT_WIDTH)
    return width, height


def recording_for(
    surfaces: list[Surface], viewport: tuple[int, int], *, least_ratio: float = 1.0, zoom: int = 100
) -> Recording | None:
    """The recording that passes the soft check (``screen_insert.MIN_DENSITY``) on every one of *surfaces*, laid out as
    a *viewport* window at *zoom* percent lays its page out; ``None`` when no recording can.

    The smallest pixel ratio a recording is made at, from *least_ratio* (one
    the author asked), on that window: the same layout, more pixels. Past
    the largest, a wider window at its proportions, zoomed in as much - its
    page laid out at least as wide as asked, never narrower, painted onto
    more pixels -, within ``spec``'s largest window and zoom. Judged by
    ``screen_insert.density`` on the frames such a recording paints, the
    gate's own measure: what it sizes is what the gate passes.
    """
    from app.services.video_studio.insert_legibility import RECORD_RATIOS
    from app.services.video_studio.spec import MAX_BROWSER_ZOOM, MAX_VIEWPORT_HEIGHT, MAX_VIEWPORT_WIDTH

    width, height = viewport

    def worst(frame: tuple[int, int]) -> screen_insert.Density | None:
        readings = [found for surface in surfaces if (found := surface.density(frame)) is not None]
        return min(readings, key=lambda found: found.value) if readings else None

    ratios = [ratio for ratio in RECORD_RATIOS if ratio >= least_ratio - 1e-9] or [RECORD_RATIOS[-1]]
    found: screen_insert.Density | None = None
    for ratio in ratios:
        frame = (round(width * ratio), round(height * ratio))
        found = worst(frame)
        if found is None:
            return None
        if not found.soft:
            return Recording((width, height), ratio, zoom, frame, found)
    ratio = ratios[-1]
    # A frame's density grows with its width: start a few pixels short of the width that reaches the floor.
    start = max(width + 1, math.floor(width * screen_insert.MIN_DENSITY / found.value) - 2)
    for wide in range(start, MAX_VIEWPORT_WIDTH + 1):
        high, grown = round(wide * height / width), math.floor(zoom * wide / width)
        if high > MAX_VIEWPORT_HEIGHT or grown > MAX_BROWSER_ZOOM:
            return None
        frame = (round(wide * ratio), round(high * ratio))
        found = worst(frame)
        if found is not None and not found.soft:
            return Recording((wide, high), ratio, grown, frame, found)
    return None


@dataclass(frozen=True)
class SoftTake:
    """A recording a screen insert draws too soft for the whole render a video is made for (:func:`soft_takes`):
    what a save refuses, with the recording that passes on every surface it is drawn on there."""

    soft: Soft
    #: The render it is judged for, and its frame.
    quality: str
    size: tuple[int, int]
    #: The recording to make (:func:`recording_for`); ``None`` when no window a recording takes passes.
    recording: Recording | None

    def words(self) -> str:
        """``its screen insert 0 draws take `t` (1280x720 px) on a quad 2400x1350 px at 3840x2160, the ultra render the
        video is made for: 0.53 source pixels per pixel ... - record it again as a 1280x720 window at pixel ratio 2 ...``."""
        from app.services.video_studio import timeline

        soft, recording = self.soft, self.recording
        source_w, source_h = soft.density.source if soft.density is not None else (0, 0)
        quad_w, quad_h = soft.surface.quad if soft.surface is not None else (0.0, 0.0)
        pushed = f", pushed in {soft.surface.push:g}x by its zoom" if soft.surface is not None and soft.surface.push > 1 else ""
        value = soft.density.value if soft.density is not None else 0.0
        head = (
            f"{timeline.insert_label(soft.where)} draws take `{soft.source[1]}` ({source_w}x{source_h} px) on a quad "
            f"{round(quad_w)}x{round(quad_h)} px{pushed} at {self.size[0]}x{self.size[1]}, the {self.quality} render the "
            f"video is made for: {value:.2f} source pixels per pixel, under {screen_insert.MIN_DENSITY:g} - a soft insert "
            "that render refuses and a smaller preview never shows"
        )
        if recording is None:
            return (
                f"{head}. No recording passes there, even a window as large as a recording takes at pixel ratio 2: give "
                "the insert a smaller quad or a smaller push"
            )
        view_w, view_h = recording.viewport
        zoom = (
            f" with a browser zoom of {recording.browser_zoom}% (its page laid out {recording.css_width} px wide)"
            if recording.browser_zoom != 100
            else ""
        )
        return (
            f"{head}. Record it again as a {view_w}x{view_h} window at pixel ratio {recording.pixel_ratio:g}{zoom}, its "
            f"frames {recording.frame[0]}x{recording.frame[1]} px ({recording.density.value:.2f} per pixel there): "
            f'record_browser_demo(target_render: "{self.quality}", video_id, scene: "{soft.scene_id}", insert: '
            f"{soft.index}) records it at that size"
        )


def soft_takes(
    spec: Any,
    size: tuple[int, int],
    quality: str,
    *,
    sources: dict[tuple[str, str], tuple[int, int]],
    footage: dict[str, tuple[int, int]],
    pixel_ratios: dict[str, float],
    zooms: dict[str, int] | None = None,
) -> list[SoftTake]:
    """The recordings *spec*'s screen inserts draw too soft at *size* - the whole render at *quality* a video is made
    for -, each with the recording that passes on every surface it is drawn on there (:func:`recording_for`):
    laid out as it was, from its own pixel ratio up. The gate's reading (:func:`soft_before`, its arguments; *zooms*
    a recording's browser zoom by id). A preview draws a source smaller than a 4K render does: what a save refuses,
    so the recording is made again once, before any preview."""
    from app.services.video_studio import timeline

    soft = [
        item
        for item in _soft_surfaces(spec, size, sources=sources, footage=footage, pixel_ratios=pixel_ratios, screens_replaced={})
        if item.source[0] == "take"
    ]
    if not soft:
        return []
    drawn: dict[str, list[Surface]] = {}
    for key, part in timeline.insert_sources(spec).items():
        if part.kind == "take":
            drawn.setdefault(part.ref, []).append(_surface(key, part, footage.get(timeline.insert_scene(key)), size))
    out = []
    for item in soft:
        ref = item.source[1]
        ratio = float(pixel_ratios.get(ref) or 1.0)
        frame_w, frame_h = sources[("take", ref)]
        window = (max(1, round(frame_w / ratio)), max(1, round(frame_h / ratio)))
        recording = recording_for(drawn[ref], window, least_ratio=ratio, zoom=(zooms or {}).get(ref, 100))
        out.append(SoftTake(item, quality, size, recording))
    return out


def soft_screens(
    document: dict,
    shots: dict[str, str],
    screens_replaced: dict[str, dict],
    size: tuple[int, int],
    *,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
) -> list[Finding]:
    """The ``INSERT_SOFT`` findings of the replaced screens the render's scenes play.

    *shots* maps a scene id to the AI shot it plays as its footage,
    *screens_replaced* a shot to its take's ``params.screen`` record
    (``screens.replace`` keeps ``picture_size``, the screenshot as fitted).
    The screen's quad is read on the start picture (``start_size``) and
    scaled to the scene's footage as the render's frame shows it at *size*
    (the footage covers the frame); the screenshot's pixels per pixel of it
    under ``screen_insert.MIN_DENSITY`` names the shot and the size it needs.
    """
    out: list[Finding] = []
    width, height = size
    for scene in document.get("scenes") or []:
        scene_id = scene.get("id")
        shot = shots.get(scene_id or "")
        record = screens_replaced.get(shot or "") if shot else None
        if not isinstance(record, dict) or not record.get("picture_size") or not record.get("quad") or not record.get("start_size"):
            continue
        start = float(scene.get("start") or 0.0)
        end = float(scene.get("end") or start)
        if span is not None and not (end > span[0] and start < span[0] + span[1]):
            continue
        start_w, start_h = (max(float(v), 1.0) for v in record["start_size"])
        src_w, src_h = float(scene.get("src_w") or start_w), float(scene.get("src_h") or start_h)
        # The footage covers the frame: its size as drawn is the frame's, or more on the side that overflows it.
        cover = max(width / max(src_w, 1.0), height / max(src_h, 1.0))
        scale_x, scale_y = cover * src_w / start_w, cover * src_h / start_h
        quad = [(float(x) * scale_x, float(y) * scale_y) for x, y in record["quad"]]
        quad_w, quad_h = quad_size(tuple(quad))
        picture_w, picture_h = (int(v) for v in record["picture_size"])
        found = screen_insert.density((picture_w, picture_h), quad_w, quad_h, "stretch")
        if found is None or not found.soft:
            continue
        fix = f"give the shot `{shot}` a screenshot at least {found.needed[0]}x{found.needed[1]} px, or render at a lower quality"
        out.append(
            Finding(
                INSERT_SOFT,
                max(0.0, start - offset),
                max(0.0, end - offset),
                scene_id,
                f"insert soft: scene {scene_id}'s replaced screen draws its screenshot ({picture_w}x{picture_h} px as "
                f"fitted) on a screen {found.quad[0]}x{found.quad[1]} px at the render's size: {found.value:.2f} source "
                f"pixels per pixel (at least {screen_insert.MIN_DENSITY:g}) - {fix}",
                FAIL,
                {"clip_id": shot, **found.as_dict()},
                fix=fix,
            )
        )
    return out


def _worst_of(frames: dict[int, dict], failing: list[int], field: str, evidence: dict, kind: str, *, lowest: bool = False) -> tuple[int, float, str | None]:
    """The worst of *failing* frames by *field* (the lowest with *lowest*), its value and the evidence kept for *kind*."""
    proof = _worst(evidence.get(kind) or [])
    worst_frame = int(proof["frame"]) if proof and int(proof["frame"]) in frames else None
    if worst_frame is None:
        worst_frame = (min if lowest else max)(failing, key=lambda n: float(frames[n].get(field) or 0.0))
    return worst_frame, float(frames[worst_frame].get(field) or 0.0), proof.get("path") if proof else None


def flicker_frames(frames: dict[int, dict]) -> list[int]:
    """The output frames where the matte's coverage or the insert's luma reversed from frame to frame.

    On the series of consecutive frames drawn, a frame reverses when the
    change into it and the change out of it have opposite signs and both are
    past the threshold - ``screen_insert.FLICKER_COVERAGE`` for the coverage,
    ``FLICKER_LUMA`` for the insert's luma less its source frame's (a cut in
    the source moves both; a matte or a despill that flickers moves one).
    Flicker is a reversal with another within :data:`FLICKER_SPAN` frames
    of it: a person walking in and out turns the coverage once, never flicker.
    """
    from app.services.video_studio import screen_insert

    ordered = sorted(frames)
    found: set[int] = set()

    def reversals(values: dict[int, float], threshold: float) -> None:
        turned: list[int] = []
        for before, now, after in zip(ordered, ordered[1:], ordered[2:], strict=False):
            if now - before != 1 or after - now != 1 or any(n not in values for n in (before, now, after)):
                continue
            into, out_of = values[now] - values[before], values[after] - values[now]
            if into * out_of < 0 and min(abs(into), abs(out_of)) > threshold:
                turned.append(now)
        for index, frame in enumerate(turned):
            neighbours = turned[max(0, index - 1) : index + 2]
            if any(other != frame and abs(other - frame) <= FLICKER_SPAN for other in neighbours):
                found.add(frame)

    reversals({n: float(a["coverage"]) for n, a in frames.items() if a.get("coverage") is not None}, screen_insert.FLICKER_COVERAGE)
    reversals(
        {
            n: float(a["luma"]) - float(a["source_luma"])
            for n, a in frames.items()
            if a.get("luma") is not None and a.get("source_luma") is not None
        },
        screen_insert.FLICKER_LUMA,
    )
    # Too much of the matte as drawn toggling back over the last two frames
    # (``screen_insert.FrameAudit.edge_hold``): the key blinks at its edge, whatever the coverage and the luma average to.
    from app.services.video_studio import screens

    found.update(n for n, a in frames.items() if a.get("edge_hold") is not None and float(a["edge_hold"]) < screens.MIN_EDGE_HOLD)
    return sorted(found)


def _alignment_of(audit: dict) -> float | None:
    """A frame's error against its bezel as the gate reads it: the bezel drawn, or the one the bezel lock's prior
    rejected for it when that is worse - a bezel that really stepped is off its quad while the prior settles -, or
    how far the bezel this frame shows has moved from under a static insert's lock (``alignment_drift``): a
    quad locked on the first frame is judged on every frame."""
    values = [float(audit[name]) for name in ("alignment", "alignment_found", "alignment_drift") if audit.get(name) is not None]
    return max(values) if values else None


def _drifted(audit: dict) -> bool:
    """Whether a frame's bezel has moved from under the lock: its drift is what puts the frame past the gate."""
    drift = audit.get("alignment_drift")
    if drift is None:
        return False
    still = max((float(audit[name]) for name in ("alignment", "alignment_found") if audit.get(name) is not None), default=0.0)
    return float(drift) > ALIGNMENT_MAX_SHARE and float(drift) > still


def _side_of(audit: dict) -> tuple[str, float] | None:
    """The side of a frame's quad farthest off its bezel (``FrameAudit.alignment_sides``), and how far; on a frame
    whose bezel moved from under the lock, the side it moved farthest on (``drift_sides``)."""
    sides = (audit.get("drift_sides") if _drifted(audit) else None) or audit.get("alignment_sides") or {}
    if not isinstance(sides, dict) or not sides:
        return None
    side = max(sides, key=lambda name: float(sides[name]))
    return side, float(sides[side])


def off_bezel(audit: dict) -> float:
    """How far a frame's quad is off its bezel as the gate reads it: the mean (:func:`_alignment_of`) or its side
    farthest off, whichever is worse."""
    side = _side_of(audit)
    return max(_alignment_of(audit) or 0.0, side[1] if side else 0.0)


def _off_bezel(frames: dict[int, dict]) -> list[int]:
    """The frames past ``surface_refine.ALIGNMENT_MAX_SHARE`` (the mean or any side, :func:`off_bezel`), in order."""
    return sorted(n for n, audit in frames.items() if off_bezel(audit) > ALIGNMENT_MAX_SHARE)


#: How far an insert's quad may jump on one frame against the frame before it, beyond the motion of the frames around
#: it, a share of the frame's width (:func:`quad_jitter`): past it the screen blinks, however close to its bezel its
#: quad stays on average.
JITTER_MAX_SHARE = 0.001
#: Two drawn quads this close at every corner (0-1) are one position: the same footage frame drawn again.
_SAME_QUAD = 1e-6


@dataclass(frozen=True)
class Jitter:
    """The worst jump of an insert's drawn quad (:func:`quad_jitter`): how far, a share of the frame's width, and the
    output frame it jumps on with the frame drawn before it."""

    share: float
    before: int
    frame: int
    #: Every output frame it jumps on past :data:`JITTER_MAX_SHARE`, in order.
    past: tuple[int, ...] = ()


def _drawn_corners(audit: dict, aspect: float) -> tuple[tuple[float, float], ...] | None:
    """The quad a frame drew its insert into (``FrameAudit.refined``), each corner in shares of the frame's width."""
    refined = audit.get("refined")
    if not isinstance(refined, list | tuple) or len(refined) != 4:
        return None
    return tuple((float(x), float(y) * aspect) for x, y in refined)


def quad_jitter(frames: dict[int, dict]) -> Jitter | None:
    """The worst jump of an insert's drawn quad from one output frame to the next (``FrameAudit.refined``): what a mean
    alignment over the frames averages away.

    A position is held to the ones around it: its displacement against the position before, less the steady motion
    through it - half the way from the position before to the one after, which a pan or a drift moves by as much -,
    mean over its four corners, a share of the frame's width. The frames showing one footage frame draw one quad (a
    footage slower than the render): one position, so a quad following its surface steps with the footage and never
    jitters. Only frames drawn one after another are compared; ``None`` with fewer than three positions in a row.
    """
    size = _size_of(frames)
    aspect = float(size[1]) / float(size[0]) if size and size[0] else 9 / 16
    #: Each run of frames drawn one after another: its positions, ``(first frame, last frame, corners)``.
    runs: list[list[tuple[int, int, tuple]]] = []
    previous = None
    for n in sorted(frames):
        corners = _drawn_corners(frames[n], aspect)
        if corners is None:
            previous = None
            continue
        if previous is None or n != previous + 1:
            runs.append([(n, n, corners)])
        elif max(abs(a - b) for p, q in zip(runs[-1][-1][2], corners, strict=True) for a, b in zip(p, q, strict=True)) <= _SAME_QUAD:
            runs[-1][-1] = (runs[-1][-1][0], n, runs[-1][-1][2])
        else:
            runs[-1].append((n, n, corners))
        previous = n
    worst: Jitter | None = None
    past: list[int] = []
    for positions in runs:
        for (_first, before, prev), (frame, _last, here), (_next, _end, after) in zip(
            positions, positions[1:], positions[2:], strict=False
        ):
            share = sum(
                math.dist(point, ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)) for point, p, q in zip(here, prev, after, strict=True)
            ) / len(here)
            if share > JITTER_MAX_SHARE:
                past.append(frame)
            if worst is None or share > worst.share:
                worst = Jitter(share, before, frame)
    return None if worst is None else Jitter(worst.share, worst.before, worst.frame, tuple(past))


def _jitter_action(frames: dict[int, dict], jumped: Jitter) -> dict:
    """The one action of an insert whose quad jumps (:func:`quad_jitter`): on a frame by a switch of its track between
    the tracker and the bezel read (``FrameAudit.track_switch``), a step no larger than the two sources can stand
    apart (``insert_tracking.BEZEL_READ_MAX_SHARE``) is the track's, never the footage's - both sources on the bezel,
    the alignment holds across it whether the footage moved or not -; else the shot made again when the bezel jumped
    with it - its error against the bezel held across the two frames, the footage itself shaking there -, else a quad
    on calmer footage or a static one, the quad having left the surface it follows."""
    from app.services.video_studio.insert_tracking import BEZEL_READ_MAX_SHARE

    at_switch = frames[jumped.before].get("track_switch") or frames[jumped.frame].get("track_switch")
    if at_switch and jumped.share <= BEZEL_READ_MAX_SHARE:
        return action(
            GIVE_QUAD,
            "the step lands where its track switches between the tracker and the bezel read, not in the footage: give "
            "the quad on another frame of the footage, or a static quad (no track) where the camera holds still",
        )
    before, after = _alignment_of(frames[jumped.before]), _alignment_of(frames[jumped.frame])
    if before is not None and after is not None and abs(after - before) < jumped.share / 2:
        return action(
            REGENERATE_SHOT,
            "the surface itself jumps between the two frames of the footage: generate the shot again with a steady camera",
        )
    return action(
        GIVE_QUAD,
        "its quad jumps off the surface it follows on that frame: give the quad on calmer footage, or a static quad (no "
        "track) where the camera holds still",
    )


#: How much of the region around an insert its jitter's evidence shows on each side, a share of the region's size.
JITTER_CROP_MARGIN = 0.25


def _side_by_side(paths: list[str], spots: list[Any], target: Any) -> bool:
    """The two encoded frames at *paths* cut to the region where the page placed the insert on either (*spots*, ``[x0,
    y0, x1, y1, shown]`` 0-1 of the frame; the whole frame when neither is known) grown by :data:`JITTER_CROP_MARGIN`,
    side by side at 100 % into *target* (a PNG), the first on the left; whether it was written."""
    from PIL import Image

    pictures = []
    for path in paths:
        with Image.open(path) as picture:
            pictures.append(picture.convert("RGB"))
    width, height = pictures[0].size
    known = [spot for spot in spots if spot is not None and len(spot) >= 4]
    if known:
        x0, y0 = min(float(s[0]) for s in known) * width, min(float(s[1]) for s in known) * height
        x1, y1 = max(float(s[2]) for s in known) * width, max(float(s[3]) for s in known) * height
        grow_x, grow_y = max((x1 - x0) * JITTER_CROP_MARGIN, 16.0), max((y1 - y0) * JITTER_CROP_MARGIN, 16.0)
        box = (max(0, int(x0 - grow_x)), max(0, int(y0 - grow_y)), min(width, int(x1 + grow_x)), min(height, int(y1 + grow_y)))
    else:
        box = (0, 0, width, height)
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return False
    cut = [picture.crop(box) for picture in pictures]
    gap = 4
    sheet = Image.new("RGB", (cut[0].width * 2 + gap, cut[0].height), (128, 128, 128))
    sheet.paste(cut[0], (0, 0))
    sheet.paste(cut[1], (cut[0].width + gap, 0))
    sheet.save(target, format="PNG")
    return True


async def jitter_proofs(
    video_path: str,
    shown: list[tuple[Finding, str | None]],
    audits: dict[str, dict],
    *,
    fps: float,
    workdir: str | Any,
    first_frame: int = 0,
) -> list[tuple[Finding, str | None]]:
    """*shown* with each ``insert_jitter`` finding's evidence: the two frames it jumps between (``data.jumped``) read back
    off the encoded *video_path*, the insert's region on both side by side at 100 % - the frame before on the left
    (:func:`_side_by_side`). *first_frame* is the video's first output frame (a scene preview's). A frame past the
    video's end, or a video that cannot be read, leaves the finding without a picture."""
    import asyncio
    from pathlib import Path

    from app.services.video_studio import encoder

    def pair_of(finding: Finding, proof: str | None) -> list[int] | None:
        jumped = finding.data.get("jumped") if finding.kind == INSERT_JITTER and proof is None else None
        return [int(n) for n in jumped] if isinstance(jumped, list) and len(jumped) == 2 else None

    wanted = sorted({n for finding, proof in shown for n in pair_of(finding, proof) or [] if n >= first_frame})
    if not wanted:
        return shown
    out_dir = Path(workdir) / "jitter"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        read = await encoder.extract_frames(video_path, [n - first_frame for n in wanted], fps=fps, out_dir=str(out_dir))
    except (OSError, RuntimeError):
        return shown
    out: list[tuple[Finding, str | None]] = []
    for index, (finding, proof) in enumerate(shown):
        pair = pair_of(finding, proof)
        paths = [read.get(n - first_frame) for n in pair] if pair else []
        if not pair or any(path is None for path in paths):
            out.append((finding, proof))
            continue
        spots = placements_of(audits.get(str(finding.data.get("insert"))) or {})
        target = out_dir / f"jitter-{index}.png"
        made = await asyncio.to_thread(_side_by_side, paths, [spots.get(n) for n in pair], target)
        out.append((finding, str(target) if made else proof))
    return out


def covers_surface(audit: dict) -> bool:
    """Whether the insert drawn on a frame covers its surface there: its quad within
    ``surface_refine.ALIGNMENT_MAX_SHARE`` of the bezel (:func:`off_bezel`) and no key left past
    ``screen_insert.RESIDUAL_FAIL_SHARE`` of it, both read. A bow it covers shows nothing of the surface - the wall past
    a curved corner kept in front, the key within the residual margin filled -: a fact, never a finding. Without both
    readings (no bezel found, an unkeyed insert) nothing says it covers."""
    if _alignment_of(audit) is None or audit.get("residual_key") is None:
        return False
    return off_bezel(audit) <= ALIGNMENT_MAX_SHARE and not residual_past(audit)


def frame_flatness(audit: dict) -> Any | None:
    """The ``surface_refine.Flatness`` of one frame's audit: the quad drawn and what its edges' points say of it;
    ``None`` when the frame read no edge."""
    from app.services.video_studio import surface_refine

    refined, size, bows = audit.get("refined"), audit.get("size"), audit.get("edge_bows")
    if not refined or not size or not bows:
        return None
    width, height = float(size[0]), float(size[1])
    corners = [(float(x) * width, float(y) * height) for x, y in refined]
    return surface_refine.flatness(
        corners, bows, audit.get("edge_inliers"), audit.get("edge_residuals"), (width, height), audit.get("edge_visible")
    )


#: The shortest a screen insert lasts (``spec.Overlay.duration``): a split never advises a part shorter.
MIN_INSERT_SECONDS = 0.3


def _corners(audit: dict | None) -> list[list[float]] | None:
    """The quad a frame's insert was drawn into, snapped to the bezel (``refined``), as four ``[x, y]`` - or ``None``."""
    refined = (audit or {}).get("refined")
    if not isinstance(refined, list) or len(refined) != 4:
        return None
    return [[round(float(x), 5), round(float(y), 5)] for x, y in refined]


def tracked_reseed(frames: dict[int, dict], off: list[int], at: Any, where: Placed | None) -> dict | None:
    """The edit that re-seeds a tracked insert whose followed corners stray on the frames *off*: the overlay fields
    to set, each one the save accepts (``spec.Overlay``), or ``None`` when the insert's spec is not known well enough.

    A track starts from the quad given on the insert's first footage frame - never from one in the middle of it
    (``quad auto`` is refused on a tracked insert). The save snaps a quad given on that frame to the bezel it measures
    there (``insert_bezels.SNAP_MAX_SHARE``), so the quad an insert stores already is those corners: giving them again
    draws the same frames and is never advised. A stray from its first frames starts the insert on the first frame
    after it instead, its source moved on by the same seconds and its quad the corners snapped there (``None`` when too
    little of it is left); a stray to its end shortens it to the frames before; a stray in the middle splits it there
    into two tracked inserts, the second starting on that frame with its source moved on by the same seconds and its
    quad the corners snapped there, from which its own track starts.
    ``{"how": "late" | "trim" | "split", "at": seconds into the insert, "inserts": [fields of each insert]}``.
    """
    timing = where.timing if where is not None else None
    # A wall's pane is one of its panes' quads, a step's time moves with its step, a push is timed in the insert:
    # none splits field by field.
    if timing is None or timing.pane is not None or timing.at_step is not None or timing.zoom_end is not None:
        return None
    into = round(max(0.0, at(off[0]) - where.start), 3)
    left = round(max(0.0, where.end - at(off[-1])), 3)
    if into < MIN_INSERT_SECONDS:
        after = next((n for n in sorted(frames) if n > off[-1]), None)
        if after is None:
            return None
        moved = round(max(0.0, at(after) - where.start), 3)
        kept = round(timing.duration - moved, 3)
        seed = _corners(frames.get(after))
        if seed is None or kept < MIN_INSERT_SECONDS:
            return None
        late: dict[str, Any] = {"start": round(timing.start + moved, 3), "duration": kept, "quad": seed}
        if timing.kind != "asset":
            late["trim_start"] = round((timing.trim_start or 0.0) + moved, 3)
        return {"how": "late", "at": moved, "inserts": [late]}
    rest = round(timing.duration - into, 3)
    if left < MIN_INSERT_SECONDS or rest < MIN_INSERT_SECONDS:
        return {"how": "trim", "at": into, "inserts": [{"duration": into}]}
    seed = _corners(frames.get(off[0]))
    if seed is None:
        return None
    second: dict[str, Any] = {"start": round(timing.start + into, 3), "duration": rest, "quad": seed, "track": True}
    if timing.kind != "asset":
        second["trim_start"] = round((timing.trim_start or 0.0) + into, 3)
    return {"how": "split", "at": into, "inserts": [{"duration": into}, second]}


def _reseed_words(edit: dict, since: str) -> str:
    """What :func:`tracked_reseed`'s *edit* tells the author to set, field by field."""
    fields = lambda part: ", ".join(f"{name}: {json.dumps(value)}" for name, value in part.items())  # noqa: E731
    if edit["how"] == "late":
        return (
            f"start it on the first frame after the stray ({fields(edit['inserts'][0])}), its track seeded there: "
            "giving its first frame's quad again changes nothing, the save snaps it to the same corners"
        )
    if edit["how"] == "trim":
        return f"end the insert before t={since} ({fields(edit['inserts'][0])})"
    first, second = edit["inserts"]
    return (
        f"split it at t={since} into two tracked inserts, as a track starts only on an insert's first frame: this one "
        f"with {fields(first)}, and a copy of it with {fields(second)}, whose track starts from those corners"
    )


def _alignment_remedy(
    frames: dict[int, dict],
    off: list[int],
    audit: dict,
    at: Any,
    saved: Any = None,
    *,
    tracked: bool = False,
    where: Placed | None = None,
) -> tuple[str, dict]:
    """``(lead, action)`` of an insert off its bezel: its one action, the quad's corners given.

    A curved surface - the worst frame's edges bowing, its points confirming it (:func:`frame_flatness`) - is
    a shot to make again, said first; one seen at an angle is drawn by the perspective warp and is no reason. A bezel
    the quad met on the insert's first frames and left later moves under it: its corners given frame by frame (track
    it); one off from the first frame is given the bezel's corners - left out so the save measures and stores them.
    A *tracked* insert already has its corners given frame by frame: it is told what moves the tracker's corners
    (its seed measured again on the frame named, a trim around the frames named) or that its drawing, snapped to the
    bezel, may stand - never the setting it already has.
    An insert the save measured (*saved*) is curved as the save measured it, never as a drawn frame reads it.
    """
    from app.services.video_studio import surface_refine

    shape = None if saved is not None else frame_flatness(audit)
    if shape is not None and not shape.flat:
        return (
            "Regenerate the shot with a flat screen: ",
            action(REGENERATE_FLAT, f"its surface is curved ({shape.words()}), which no planar insert covers"),
        )
    first = min(frames)
    if tracked:
        # Followed frame by frame already: the tracker's corners strayed from the bezel on the frames named. Its track
        # starts only from the quad given on its first frame, so the edit named is one the save takes.
        since, until = clock(at(off[0])), clock(at(off[-1]))
        edit = tracked_reseed(frames, off, at, where)
        how = (
            _reseed_words(edit, since)
            if edit is not None
            else f"split it at t={since} into two tracked inserts, the second starting there with its trim_start moved on "
            "by the same seconds and its quad the corners measured on that frame (a track starts only on an insert's "
            "first frame), or trim the insert around those frames - never its first frame's quad given again, which "
            "the save snaps to the same corners"
        )
        chosen = action(
            GIVE_QUAD,
            f"it is tracked already and its followed corners stray from the bezel from t={since} to t={until}: {how}; "
            "drawn snapped to the bezel, it may also be accepted as it is",
        )
        if edit is not None:
            chosen["edit"] = edit
        return "", chosen
    if any(_drifted(frames[n]) for n in off):
        # The bezel moved from under a quad locked on the first frame: the footage moves, not the quad.
        moved = next(n for n in off if _drifted(frames[n]))
        return "", action(
            GIVE_QUAD,
            f"the bezel moves from under the locked quad from t={clock(at(moved))} (the shot's camera or its surface drifts): "
            "track it (track: true), or generate the shot again locked off",
        )
    if off[0] - first >= surface_refine.ALIGNMENT_MIN_RUN:
        return "", action(
            GIVE_QUAD, f"the surface moves under it from t={clock(at(off[0]))}: give its corners frame by frame (track: true)"
        )
    return "", action(
        GIVE_QUAD,
        "leave its quad out (quad auto) and save: the bezel's corners are measured on the footage frame it comes in on "
        "and stored",
    )


def _curved(shape: Any, audit: dict) -> bool:
    """Whether a frame's surface *shape* fails the flatness gate: not flat, and not a bow the insert drawn
    on that frame covers (:func:`covers_surface`) - a quad that is not convex never is one."""
    return not shape.flat and not (shape.convex and covers_surface(audit))


def gate_frames(frames: dict[int, dict], saved: Any = None) -> dict[str, list[int]]:
    """The frames failing each frame-by-frame gate measured on an insert's audited *frames*: what
    :func:`_compositing_findings` reads and what the report's ``verdicts`` say (:func:`verdicts`). The flatness of an
    insert the save measured (*saved*, ``insert_bezels.Measured``) is the save's on every frame."""
    from app.services.video_studio import insert_bezels, screen_insert

    audits = list(frames.values())
    out: dict[str, list[int]] = {}
    if any(_alignment_of(a) is not None for a in audits):
        out["alignment"] = _off_bezel(frames)
    if (jumped := quad_jitter(frames)) is not None:
        out["jitter"] = list(jumped.past)
    shapes = {n: shape for n, a in frames.items() if (shape := frame_flatness(a)) is not None}
    if saved is not None:
        out["flatness"] = sorted(frames) if insert_bezels.flat_problem(saved) is not None else []
    elif shapes:
        out["flatness"] = sorted(n for n, shape in shapes.items() if _curved(shape, frames[n]))
    if any(a.get("residual_key") is not None for a in audits):
        out["residual_key"] = sorted(n for n, a in frames.items() if residual_past(a))
    if any(a.get("halo") is not None for a in audits):
        out["halo"] = sorted(n for n, a in frames.items() if a.get("halo") is not None and a["halo"] > screen_insert.MAX_HALO_SHARE)
    if any(a.get("rim") is not None or a.get("rim_off") is not None for a in audits):
        out["rim"] = sorted(n for n, a in frames.items() if rim_past(a))
    if any(a.get("black_lift") is not None for a in audits):
        out["colour_shift"] = sorted(n for n, a in frames.items() if colour_bad(a) > 1.0)
    if any(a.get("sharpness") is not None for a in audits):
        out["sharpness"] = sorted(
            n for n, a in frames.items() if a.get("sharpness") is not None and a["sharpness"] < screen_insert.MIN_SHARPNESS
        )
    out["flicker"] = flicker_frames(frames)
    return out


def rim_past(audit: dict) -> bool:
    """Whether a frame's audit fails the grey-rim gate on its own: past ``screen_insert.MAX_RIM_SHARE`` of the band on
    the keyed surface (``rim``), or of the band off it the compositing made grey where the footage had colour
    (``rim_off``) - a subject's fringe the key's clamp turned grey over the insert."""
    return any(
        audit.get(name) is not None and float(audit[name]) > screen_insert.MAX_RIM_SHARE for name in ("rim", "rim_off")
    )


def residual_past(audit: dict) -> bool:
    """Whether a frame's audit fails the residual-key gate on its own: past ``screen_insert.RESIDUAL_FAIL_SHARE`` of the
    quad, a seam of the key's hue straight across it at any share (``FrameAudit.residual_seam``), a pocket of
    the key left beside what stands in front at any share (``FrameAudit.residual_pocket``), or a line of it along an
    edge of the quad at any share (``FrameAudit.residual_edge``)."""
    return (
        bool(audit.get("residual_seam"))
        or bool(audit.get("residual_pocket"))
        or bool(audit.get("residual_edge"))
        or float(audit.get("residual_key") or 0.0) > screen_insert.RESIDUAL_FAIL_SHARE
    )


def gate_fails(gate: str, failing: list[int], frames: int, *, joined: list[int] | None = None) -> bool:
    """Whether *gate*, failing on the frames *failing* of an insert's *frames* drawn, fails it: the alignment and
    the flatness on ``surface_refine.PERSIST_FRAMES`` consecutive frames, the residual key, the halo, the grey rim
    and the colours on :data:`MIN_FAILING_FRAMES` frames, the sharpness, the flicker and the jitter on any - and the
    residual key on any one frame of its scene's joins with another (*joined*): there the key shows through a
    transition or a cut, where no later frame hides it."""
    from app.services.video_studio.surface_refine import persistent

    if gate in ("alignment", "flatness"):
        return persistent(failing, frames)
    if gate == "residual_key" and joined:
        return True
    if gate in ("residual_key", "halo", "rim", "colour_shift"):
        return enough(failing, frames)
    return bool(failing)


def verdicts(
    frames: dict[int, dict], where: Placed | None = None, saved: Any = None, placements: dict[int, Any] | None = None
) -> dict[str, bool]:
    """``{gate: passed}`` of each frame-by-frame gate measured on an insert's audited *frames*: kept on its
    report and its scene's drawing - what a later drawing of the same shot and spec is held to (:func:`disagreeing`).
    *where* places it: a residual key on a frame of its scene's joins fails it (:func:`gate_fails`), its
    flicker is read on the frames that are its scene's (:meth:`Placed.owns_frame`, *placements*); *saved* is
    what the save measured of it, whose flatness it takes (:func:`gate_frames`)."""
    failing = gate_frames(frames, saved)
    if where is not None:
        failing["flicker"] = [n for n in failing["flicker"] if where.owns_frame(n, placements)]
    return {
        gate: not gate_fails(gate, frames_failing, len(frames), joined=[n for n in frames_failing if where is not None and where.at_join(n)])
        for gate, frames_failing in failing.items()
    }


def missing_frames(
    where: Placed, frames: dict[int, dict], frame_range: tuple[int, int] | None = None, placements: dict[int, Any] | None = None
) -> list[int]:
    """The output frames the page draws an insert's layer on that the frame server drew no frame of: the page
    log's account of an insert present on one frame and absent on the next - its keyed surface left raw there. Judged
    on an insert drawn at least once (*frames*, its audits), on the frames that are its scene's
    (:meth:`Placed.judged_frames`: *placements* where the page placed it, :func:`placements_of`), within *frame_range*
    (a scene preview's) when given. Never a finding on its own: the presence gate says those of them the
    encoded frame shows it missing on, shown whole (:meth:`PresenceGate.result`)."""
    if not frames:
        return []
    return [frame for frame in where.judged_frames(placements, frame_range) if frame not in frames]


def owned_flicker(where: Placed, frames: dict[int, dict], placements: dict[int, Any] | None = None) -> list[int]:
    """The frames an insert's matte's coverage or luma reverses on (:func:`flicker_frames`), of those that are its
    scene's (:meth:`Placed.owns_frame`): its flicker findings' frames. Its flicker fact (``flicker_frames`` of
    its report) counts them and the frames its layer was not drawn on, which the presence gate reads off the encoded
    frames (``presence.undrawn``): a note of either never stands beside a fact of 0 (:func:`contradictions`)."""
    return [frame for frame in flicker_frames(frames) if where.owns_frame(frame, placements)]


def _flatness_read(shape: Any) -> str:
    """``its top edge bows 8% of its length: 100% of it visible, 31% of its points on one line, 0.82% of the frame's
    width off one straight line`` - a confirmed bow with the three readings that confirm it."""
    from app.services.video_studio.surface_refine import SIDES

    confirmed = [index for index in range(len(SIDES)) if shape.confirmed(index)]
    if not confirmed:
        return shape.words()
    index = max(confirmed, key=lambda i: float(shape.bows[i] or 0.0))
    parts = []
    if shape.visible[index] is not None:
        parts.append(f"{float(shape.visible[index]):.0%} of it visible")
    if shape.inliers[index] is not None:
        parts.append(f"{float(shape.inliers[index]):.0%} of its points on one line")
    if shape.residuals[index] is not None:
        parts.append(f"{float(shape.residuals[index]) * 100:.2f}% of the frame's width off one straight line")
    return f"{shape.words()}: {', '.join(parts)}" if parts else shape.words()


def _compositing_findings(
    key: str,
    where: Placed,
    frames: dict[int, dict],
    evidence: dict,
    at: Any,
    earlier: list[dict] | None = None,
    flicker: list[int] | None = None,
    *,
    saved: Any = None,
) -> list[tuple[Finding, str | None]]:
    """The findings for one insert: curved, off its bezel, residual key, flicker, soft, haloed, rimmed
    in grey - each
    on the frames its gate needs (:func:`gate_fails`), with its one action. *earlier* is what the video's kept
    drawings measured of it, newest first (``render_checks.earlier_measures``); *flicker* the frames its matte
    reverses on, of those that are its scene's (:func:`owned_flicker`); *saved* what the save measured of it
    (``insert_bezels.Measured``), whose flatness it takes. Its layer not drawn on a frame the page draws it on
    is the presence gate's to say, off the encoded frame (:meth:`PresenceGate.result`)."""
    from app.services.video_studio import screen_insert
    from app.services.video_studio.surface_refine import FLAT_MAX_SHARE

    out: list[tuple[Finding, str | None]] = []
    total = len(frames)
    failing = gate_frames(frames, saved)
    if flicker is not None:
        failing["flicker"] = flicker
    off, residue = failing.get("alignment", []), failing.get("residual_key", [])
    # A residual key on a frame of its scene's joins blocks on that one frame: through a transition or a cut.
    joined = [n for n in residue if where.at_join(n)]
    misaligned = gate_fails("alignment", off, total)
    keyed_residue = gate_fails("residual_key", residue, total, joined=joined)
    # A curved surface: an edge bowing past FLAT_MAX_SHARE on its visible part, its points confirming it,
    # on PERSIST_FRAMES frames in a row, that the insert drawn there does not cover - or as the save measured it
    # on the footage frame, once. Its quad off the bezel and the key left past its edge follow from it: one finding, the
    # shot.
    curved = failing.get("flatness", [])
    folded: list[str] = []
    if gate_fails("flatness", curved, total):
        if saved is not None:
            worst_frame, shape = curved[0], saved.bezel.flatness
            read = f" as the save measured it on {saved.where} ({_flatness_read(shape)}, past {FLAT_MAX_SHARE:.0%})"
        else:
            shapes = {n: frame_flatness(frames[n]) for n in curved}
            worst_frame = max(curved, key=lambda n: shapes[n].error)
            shape = shapes[worst_frame]
            read = (
                f" ({_flatness_read(shape)}; {len(curved)} frame{'s' if len(curved) != 1 else ''} past "
                f"{FLAT_MAX_SHARE:.0%})"
            )
        if misaligned:
            folded.append(INSERT_ALIGNMENT)
        if keyed_residue and not joined:
            folded.append(INSERT_RESIDUAL_KEY)
        follows = (
            f"; its {' and '.join('quad off the bezel' if kind == INSERT_ALIGNMENT else 'key left past its edge' for kind in folded)} "
            "follow from it"
            if folded
            else ""
        )
        covered = f"; {shape.covered_words()}, not judged" if shape.covered_words() else ""
        chosen = action(REGENERATE_FLAT, "generate the shot again with a flat screen: a planar insert cannot cover a curved one")
        out.append(
            (
                Finding(
                    INSERT_FLATNESS,
                    at(curved[0]),
                    at(curved[-1]),
                    where.scene_id,
                    f"Regenerate the shot with a flat screen: insert on a curved surface at t={clock(at(worst_frame))}: "
                    f"{where.label}'s surface is curved{read}{covered}{follows} - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(curved),
                        "worst": round(shape.error, 4),
                        "worst_at": round(at(worst_frame), 3),
                        "follows": folded,
                        "edges": shape.edges(),
                        **({"covered": shape.covered()} if shape.covered() else {}),
                        **({"measured": "save"} if saved is not None else {}),
                        **_gate_data("flatness", frames[worst_frame], shape.error, FLAT_MAX_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                None,
            )
        )
    if misaligned and INSERT_ALIGNMENT not in folded:
        worst_frame, worst, proof = _worst_of(frames, off, "alignment", evidence, "alignment")
        if proof is None and any(_drifted(frames[n]) for n in off):
            # As the gate reads it: on a bezel that moved from under the lock, the frame it moved farthest on;
            # a quad off its bezel from the first frame keeps the lock's own worst frame.
            worst_frame = max(off, key=lambda n: off_bezel(frames[n]))
        audit = frames[worst_frame]
        worst = max(worst, _alignment_of(audit) or 0.0)
        refined = audit.get("refined")
        lead, chosen = _alignment_remedy(frames, off, audit, at, saved, tracked=where.tracked, where=where)
        sides = (audit.get("drift_sides") if _drifted(audit) else audit.get("alignment_sides")) or {}
        side = _side_of(audit)
        by_side = f", its {side[0]} side {side[1] * 100:.2f}% off" if side else ""
        out.append(
            (
                Finding(
                    INSERT_ALIGNMENT,
                    at(off[0]),
                    at(off[-1]),
                    where.scene_id,
                    f"{lead}insert off its bezel at t={clock(at(worst_frame))}: {where.label}'s quad is {worst * 100:.2f}% of the "
                    f"frame's width off the bezel the footage shows{by_side} ({len(off)} frame{'s' if len(off) != 1 else ''} past "
                    f"{ALIGNMENT_MAX_SHARE * 100:.1f}%, its mean or one side); the insert is drawn snapped to the bezel - "
                    f"{chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(off),
                        "worst": round(worst, 5),
                        "worst_at": round(at(worst_frame), 3),
                        "refined": refined,
                        **({"sides": sides} if sides else {}),
                        **({"drifted": True, "bezel_found": audit.get("bezel_found")} if _drifted(audit) else {}),
                        **_gate_data("alignment", audit, off_bezel(audit), ALIGNMENT_MAX_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                proof,
            )
        )
    # A quad jumping on one frame against the frame before it: a blinking screen its mean alignment averages away. Its
    # two frames are cut from the encoded video once it is written (:func:`jitter_proofs`).
    jumped = quad_jitter(frames)
    if jumped is not None and gate_fails("jitter", list(jumped.past), total):
        chosen = _jitter_action(frames, jumped)
        moved = jumped.share * 100
        out.append(
            (
                Finding(
                    INSERT_JITTER,
                    at(jumped.past[0]),
                    at(jumped.past[-1]),
                    where.scene_id,
                    f"insert jitters: {moved:.2g}% between {at(jumped.before):.3f} s and {at(jumped.frame):.3f} s: "
                    f"{where.label}'s quad moves {moved:.2f}% of the frame's width against the frame before, beyond the "
                    f"motion of the frames around it ({len(jumped.past)} frame{'s' if len(jumped.past) != 1 else ''} past "
                    f"{JITTER_MAX_SHARE * 100:.1f}%) - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(jumped.past),
                        "worst": round(jumped.share, 5),
                        "worst_at": round(at(jumped.frame), 3),
                        "before_at": round(at(jumped.before), 3),
                        "jumped": [jumped.before, jumped.frame],
                        **_gate_data("jitter", frames[jumped.frame], jumped.share, JITTER_MAX_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                None,
            )
        )
    # Past RESIDUAL_FAIL_SHARE of the quad on MIN_FAILING_FRAMES frames: under it, a few pixels nobody
    # sees, said in the insert's report.
    if keyed_residue and INSERT_RESIDUAL_KEY not in folded:
        worst_frame, worst, proof = _worst_of(frames, residue, "residual_key", evidence, "residual")
        if joined and worst_frame not in joined:
            # Named by the frame of a join it blocks on, its own worst frame's evidence kept only if it is that one.
            worst_frame = max(joined, key=lambda n: float(frames[n].get("residual_key") or 0.0))
            worst, proof = float(frames[worst_frame]["residual_key"]), None
        sits = frames[worst_frame].get("residual_where") or None
        said = residual_where_text(sits)
        # The same residual on an earlier drawing at another key or light: no setting moves it.
        still = unchanged(where, "residual_key", max(float(frames[n]["residual_key"]) for n in residue), earlier, inside=inside_share(sits))
        chosen = _residual_action(sits, still=still, now=where)
        # A seam straight across the quad: the compositing's, no key, light or shot moves it.
        seamed = [n for n in residue if frames[n].get("residual_seam")]
        if seamed:
            chosen = action(NOTHING, "nothing in the video causes it: report the render with this frame")
        through = (
            f"; {len(joined)} on its scene's join with another (a transition or a cut), where one frame blocks" if joined else ""
        )
        if seamed:
            through += (
                f"; a straight run of the key's colour across its quad on {len(seamed)} frame{'s' if len(seamed) != 1 else ''}, "
                "a seam of the compositing that fails whatever its share"
            )
        # A pocket of the key left beside what stands in front: the matte's, no key setting moves it.
        pocketed = [n for n in residue if frames[n].get("residual_pocket")]
        if pocketed:
            through += (
                f"; a pocket of the key's colour left beside what stands in front on {len(pocketed)} "
                f"frame{'s' if len(pocketed) != 1 else ''}, which fails whatever its share"
            )
        # A line of the key along a side of the quad: the share of the quad it holds never fails a frame.
        edged = [n for n in residue if frames[n].get("residual_edge")]
        edges: dict[str, int] = {}
        for n in edged:
            for side, wide in (frames[n].get("residual_edge") or {}).items():
                edges[str(side)] = max(edges.get(str(side), 0), int(wide))
        if edges:
            sides = ", ".join(f"{side} {wide} px wide" for side, wide in edges.items())
            through += (
                f"; a line of the key's colour along its edge ({sides}) on {len(edged)} "
                f"frame{'s' if len(edged) != 1 else ''}, which fails whatever its share"
            )
        out.append(
            (
                Finding(
                    INSERT_RESIDUAL_KEY,
                    at(residue[0]),
                    at(residue[-1]),
                    where.scene_id,
                    f"residual key at t={clock(at(worst_frame))}: {where.label} leaves the key's colour on {worst * 100:.2f}% "
                    f"of its quad, inside it or within {screen_insert.RESIDUAL_MARGIN * 100:.1f}% of the frame's width of "
                    f"it ({len(residue)} frame{'s' if len(residue) != 1 else ''} past "
                    f"{screen_insert.RESIDUAL_FAIL_SHARE * 100:.1f}%{through}){f': {said}' if said else ''} - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(residue),
                        "worst": round(worst, 5),
                        "worst_at": round(at(worst_frame), 3),
                        "frame": worst_frame,
                        **({"transition": len(joined)} if joined else {}),
                        **({"seam": len(seamed)} if seamed else {}),
                        **({"pocket": len(pocketed)} if pocketed else {}),
                        **({"edge": edges} if edges else {}),
                        **({"where": sits} if sits else {}),
                        **({"unchanged": still} if still else {}),
                        **_gate_data("residual_key", frames[worst_frame], worst, screen_insert.RESIDUAL_FAIL_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                proof,
            )
        )
    flicker = failing.get("flicker", [])
    if flicker:
        still = unchanged(where, "flicker", float(len(flicker)), earlier)
        cause = "the footage under the insert changes from frame to frame"
        fix = "generate the shot again with the surface evenly lit"
        chosen = action(REGENERATE_SHOT, _not_a_setting(FOREGROUND, f"{cause} - {fix}", still, where) if still else f"{cause}: {fix}")
        out.append(
            (
                Finding(
                    INSERT_FLICKER,
                    at(flicker[0]),
                    at(flicker[-1]),
                    where.scene_id,
                    f"insert flickers at t={clock(at(flicker[0]))}: {where.label}'s matte coverage or luma reverses from "
                    f"frame to frame, or its matte toggles back at its edge, on {len(flicker)} "
                    f"frame{'s' if len(flicker) != 1 else ''} (past {screen_insert.FLICKER_COVERAGE * 100:.0f}% of the "
                    f"quad or {screen_insert.FLICKER_LUMA:.0f} grey levels, or over {1.0 - screens.MIN_EDGE_HOLD:.0%} of the "
                    f"matte toggling back) - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(flicker),
                        "first_at": round(at(flicker[0]), 3),
                        **({"unchanged": still} if still else {}),
                        **_gate_data("flicker", frames[flicker[0]], len(flicker), 0, chosen),
                    },
                    fix=chosen["text"],
                ),
                None,
            )
        )
    soft = failing.get("sharpness", [])
    if gate_fails("sharpness", soft, total):
        worst_frame, worst, proof = _worst_of(frames, soft, "sharpness", evidence, "soft", lowest=True)
        chosen = action(REPLACE_SOURCE, "use a source at least the size of the quad at the render's resolution, or a sharper one")
        out.append(
            (
                Finding(
                    INSERT_SOFT,
                    at(soft[0]),
                    at(soft[-1]),
                    where.scene_id,
                    f"insert soft at t={clock(at(worst_frame))}: {where.label} is drawn at {worst:.2f} of its source's "
                    f"sharpness ({len(soft)} frame{'s' if len(soft) != 1 else ''} under {screen_insert.MIN_SHARPNESS:.2f}) - "
                    f"{chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(soft),
                        "worst": round(worst, 4),
                        "worst_at": round(at(worst_frame), 3),
                        **_gate_data("sharpness", frames[worst_frame], worst, screen_insert.MIN_SHARPNESS, chosen),
                    },
                    fix=chosen["text"],
                ),
                proof,
            )
        )
    # On MIN_FAILING_FRAMES frames at least: a strand crossing the edge for a frame or two is never a halo.
    halo = failing.get("halo", [])
    if gate_fails("halo", halo, total):
        worst_frame, worst, proof = _worst_of(frames, halo, "halo", evidence, "halo")
        own = float(frames[worst_frame].get("halo_own") or 0.0)
        edge = frames[worst_frame].get("halo_edge")
        clamped = float(frames[worst_frame].get("halo_clamped") or 0.0)
        # What the footage itself shows of that hue on the edge is left out of it: said, never blamed; what was
        # counted where the compositor's clamp touched the edge is said apart: the clamp's colour, not the shot's.
        mine = f"; {own * 100:.1f}% of the edge is the footage's own colour there and is not counted" if own > 0 else ""
        clamp = f"; {clamped * 100:.1f}% of the edge is counted where the compositor's clamp touched it" if clamped > 0 else ""
        still = unchanged(where, "halo", max(float(frames[n]["halo"]) for n in halo), earlier)
        spill, chosen = _halo_said(where, still, clamp=clamped * 2.0 >= worst, hue=frames[worst_frame].get("halo_hue"))
        cropped = "; its worst stretch kept at 100%" if (evidence.get("halo_crop") or []) else ""
        out.append(
            (
                Finding(
                    INSERT_HALO,
                    at(halo[0]),
                    at(halo[-1]),
                    where.scene_id,
                    f"insert halo at t={clock(at(worst_frame))}: {where.label}: edge spill {worst * 100:.1f}%"
                    f"{f' {spill}' if spill else ''} on the foreground's edge{f' ({int(edge)} px long)' if edge else ''}, "
                    f"the key's hue or its complement left on it ({len(halo)} frame{'s' if len(halo) != 1 else ''} past "
                    f"{screen_insert.MAX_HALO_SHARE * 100:.0f}%{mine}{clamp}{cropped}) - {chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(halo),
                        "worst": round(worst, 4),
                        "worst_at": round(at(worst_frame), 3),
                        **({"edge_px": int(edge)} if edge else {}),
                        # The worst frame's edge told apart: what was counted, left out as the subject's own
                        # colour, and counted where the clamp touched.
                        "parts": _halo_parts(frames[worst_frame]),
                        **({"despill": where.despill} if where.despill is not None else {}),
                        **({"unchanged": still} if still else {}),
                        **_gate_data("halo", frames[worst_frame], worst, screen_insert.MAX_HALO_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                proof,
            )
        )
    # A grey band past the subject, on MIN_FAILING_FRAMES frames: no hue outlier, so never the halo's.
    rim = failing.get("rim", [])
    if gate_fails("rim", rim, total):
        # Read on the keyed surface, or - none past the gate there - off it, where the compositing made it grey.
        off_only = not any(float(frames[n].get("rim") or 0.0) > screen_insert.MAX_RIM_SHARE for n in rim)
        field = "rim_off" if off_only else "rim"
        worst_frame, worst, proof = _worst_of(frames, rim, field, evidence, "rim")
        edge = frames[worst_frame].get("rim_edge")
        light = float(frames[worst_frame].get("rim_light") or 0.0)
        # The share on the keyed surface (gated) and on the footage's own surroundings (a fact).
        around = frames[worst_frame].get("rim_surroundings")
        apart = f"; {float(around) * 100:.1f}% on the surroundings off the keyed surface, not gated" if around is not None else ""
        off = frames[worst_frame].get("rim_off")
        still = unchanged(where, "rim", max(float(frames[n].get(field) or 0.0) for n in rim), earlier)
        chosen = _rim_action(where, still, light=light, off=off_only)
        surface = (
            "off the keyed surface, where the footage had colour and the compositing drew grey over the insert"
            if off_only
            else "over the keyed surface"
        )
        cropped = "; its worst stretch kept at 100%" if (evidence.get("rim_crop") or []) else ""
        lit = f"; {light * 100:.0f}% of it where the insert's light took its colour" if light > 0 else ""
        out.append(
            (
                Finding(
                    INSERT_RIM,
                    at(rim[0]),
                    at(rim[-1]),
                    where.scene_id,
                    f"insert rim at t={clock(at(worst_frame))}: {where.label}: grey on {worst * 100:.1f}% of the band "
                    f"{screen_insert.RIM_FROM_PX:g}-{screen_insert.RIM_PX:g} px past what stands in front {surface}"
                    f"{f' ({int(edge)} px of its edge)' if edge else ''}, where the insert is not ({len(rim)} "
                    f"frame{'s' if len(rim) != 1 else ''} past {screen_insert.MAX_RIM_SHARE * 100:.0f}%{apart}{lit}{cropped}) - "
                    f"{chosen['text']}",
                    FAIL,
                    {
                        "insert": key,
                        "frames": len(rim),
                        "worst": round(worst, 4),
                        "worst_at": round(at(worst_frame), 3),
                        **({"edge_px": int(edge)} if edge else {}),
                        "light": round(light, 4),
                        "rim_on_surface": round(float(frames[worst_frame].get("rim") or 0.0), 4),
                        "rim_on_surroundings": round(float(around), 4) if around is not None else None,
                        "rim_off_surface": round(float(off), 4) if off is not None else None,
                        **({"unchanged": still} if still else {}),
                        **_gate_data("rim", frames[worst_frame], worst, screen_insert.MAX_RIM_SHARE, chosen),
                    },
                    fix=chosen["text"],
                ),
                proof,
            )
        )
    return out


def _rim_action(where: Placed, still: dict | None, *, light: float, off: bool = False) -> dict:
    """The one action of a grey rim: the insert's light lowered when that light made most of the rim grey (its
    worst frame's ``rim_light``) and an earlier drawing at another light did not leave the same rim; else the shot made
    again - the footage's own edge leaves the key's colour there, which no setting moves (``not a setting:`` when an
    earlier drawing at another key, light or despill left the same, *still*, :func:`unchanged`). A rim read off the
    keyed surface (*off*: the subject's fringe the key reads clear, its key colour clamped grey) with the edge despill
    off turns it on first."""
    if off and still is None and where.despill is not None and not screen_insert.Despill.of(where.despill).edge:
        return action(
            WIDEN_DESPILL,
            "turn the edge despill on (despill: {edge: true, width_px: 4}): the subject's fringe keeps the key's colour, "
            "which the clamp turns grey over the insert",
        )
    if light >= 0.5 and still is None and where.light:
        return action(
            LOWER_LIGHT,
            f"lower its light (light: {float(where.light):g} now; {INSERT_DEFAULT_LIGHT:g} is the default, 0 draws the "
            "source as it is): the footage's light laid over the insert takes its colour along the edge of what stands "
            "in front",
        )
    cause = "the edge of what stands in front holds the key's colour in the footage itself"
    regenerate = "generate the shot again with what stands in front lit apart from the surface"
    if still is not None:
        return action(REGENERATE_SHOT, _not_a_setting(FOREGROUND, f"{cause} - {regenerate}", still, where))
    return action(REGENERATE_SHOT, f"{cause}: {regenerate}")


def _halo_parts(audit: dict) -> dict:
    """One frame's halo told apart: ``{edge_px, spill, own, clamped, hue?}`` - the edge's length, the share
    counted, the share left out as the subject's own colour and, of what was counted, the share where the compositor's
    clamp touched the edge, with the hue counted (its name)."""
    parts = {
        "edge_px": int(audit.get("halo_edge") or 0),
        "spill": round(float(audit.get("halo") or 0.0), 4),
        "own": round(float(audit.get("halo_own") or 0.0), 4),
        "clamped": round(float(audit.get("halo_clamped") or 0.0), 4),
    }
    named = screen_insert.hue_name(audit.get("halo_hue"))
    return {**parts, "hue": named} if named else parts


def _halo_said(where: Placed, still: dict | None, *, clamp: bool = False, hue: float | None = None) -> tuple[str, dict]:
    """How a halo finding says the edge despill it was measured after, and its one action: ``after despill width
    2 px`` and the despill widened - turned on when it is off -; past ``spec.MAX_INSERT_DESPILL_WIDTH``, ``not a
    setting: foreground`` and the shot made again. When an earlier drawing at another key, light or despill left the
    same halo (*still*, :func:`unchanged`), no setting is what leaves it: the foreground's own colour - *hue*,
    the hue counted, by its name -, which the author declares (``despill.own_hues``) or accepts; the shot made again
    when most of it was counted where the compositor's clamp touched the edge (*clamp*), a colour the shot never had."""
    from app.services.video_studio.spec import MAX_INSERT_DESPILL_WIDTH

    cause = "the edge of what stands in front holds that hue in the footage itself"
    regenerate = "generate the shot again with what stands in front lit apart from the surface"
    despill = screen_insert.Despill.of(where.despill)
    if where.despill is None:
        spill = ""
    elif not despill.edge:
        spill = "with the edge despill off (despill.edge: false)"
    else:
        spill = f"after despill width {despill.width_px:g} px" + (f" at strength {despill.strength:g}" if despill.strength < 1.0 else "")
    if still is not None and not clamp:
        named = screen_insert.hue_name(hue)
        return spill, action(DECLARE_OWN_HUES, _not_a_setting(OWN_COLOUR, "declare it with insert.despill.own_hues or accept", still, where, named=named))
    if still is not None:
        clamped = "the key's colour on the edge of what stands in front, which the compositor's clamp turns"
        return spill, action(REGENERATE_SHOT, _not_a_setting(FOREGROUND, f"{clamped} - {regenerate}", still, where))
    if where.despill is not None and not despill.edge:
        return spill, action(WIDEN_DESPILL, "turn the edge despill on (despill: {edge: true}) before generating the shot again")
    if where.despill is not None and (despill.width_px < MAX_INSERT_DESPILL_WIDTH or despill.strength < 1.0):
        wider = min(MAX_INSERT_DESPILL_WIDTH, max(2.0 * despill.width_px, despill.width_px + 2.0))
        full = "" if despill.strength >= 1.0 else ", its strength to 1"
        return spill, action(
            WIDEN_DESPILL,
            f"raise despill.width_px ({despill.width_px:g} now; {wider:g} reaches farther into the edge{full}) before "
            "generating the shot again",
        )
    return spill, action(REGENERATE_SHOT, f"{cause}: {regenerate}")


#: Where a residual key sits (``screen_insert.RESIDUAL_REGIONS``), in words, and what changes it.
_RESIDUAL_PLACES = {
    "inside": "inside the quad along its {side}",
    "contain": "in the contain bands inside the quad along its {side}",
    "edge": "on the surface running past the quad on its {side}",
    "band": "in the margin past the bezel on its {side}",
}
#: Where a residual key sits, what leaves it there and its one action: ``(action, cause, what, fix)`` - the
#: cause one of :data:`NOT_A_SETTING_CAUSES`, said when no key or light setting moved it (:func:`unchanged`). No key
#: tolerance is advised: on the fixtures none moves a residual the shared masks leave (``screen_insert.GateMasks``).
_RESIDUAL_ACTIONS = {
    "inside": (
        REPLACE_SOURCE,
        SOURCE_CONTENT,
        "the surface showing through the source's transparent pixels inside the quad",
        "give the insert a picture opaque there",
    ),
    "contain": (
        REPLACE_SOURCE,
        SOURCE_CONTENT,
        "the surface showing through the contain bands",
        "use a source of the quad's proportions, or a picture opaque to its edges so the bands are filled",
    ),
    "edge": (
        GIVE_QUAD,
        PAST_BEZEL,
        "the surface running past the quad",
        "leave its quad out (quad auto, no track) and save, so the save measures the surface's bezel and stores its "
        "corners",
    ),
    "band": (
        REGENERATE_SHOT,
        PAST_BEZEL,
        "the footage's own key colour past the bezel, which no quad covers",
        "generate the shot again with the screen one flat colour inside its bezel",
    ),
}
#: Two findings whose value is the same to this many decimals are the same finding (:func:`unchanged`).
UNCHANGED_DECIMALS = 3
#: What filled a contain fit's bands (``screen_insert.BAND_FILLS``), in words.
_BAND_FILL_WORDS = {
    "source edge": "filled with the source's own edge colour {colour}",
    "bezel": "filled with the bezel's colour {colour}",
    "black": "filled with black",
    "surface": "showing the keyed surface (the source's edge is transparent)",
    "footage": "showing the footage (no key)",
}


def band_fill_text(band: dict | None) -> str:
    """``contain bands 24.8% of the quad, filled with the source's own edge colour #181C24``; empty for none."""
    if not isinstance(band, dict) or not band.get("share"):
        return ""
    words = _BAND_FILL_WORDS.get(str(band.get("filled_with")), "filled with {colour}")
    return f"contain bands {float(band['share']) * 100:.1f}% of the quad, " + words.format(colour=band.get("colour") or "")


def inside_share(where: dict | None) -> float:
    """How much of a residual key sits inside the quad - where the source is drawn and in the contain bands - a share of
    the quad (``FrameAudit.residual_where``)."""
    return round(sum(float(share) for region in ("inside", "contain") for share in ((where or {}).get(region) or {}).values()), 5)


def _settings_text(settings: dict | None, light: float | None = None, despill: dict | None = None) -> str:
    keyed = (
        f"key {settings.get('color')}, tolerance {float(settings.get('tolerance') or 0.0):g}, feather {float(settings.get('feather') or 0.0):g}"
        if isinstance(settings, dict)
        else "no key"
    )
    if isinstance(settings, dict) and isinstance(despill, dict):
        drawn = screen_insert.Despill.of(despill)
        keyed += f", despill width {drawn.width_px:g} strength {drawn.strength:g}" if drawn.edge else ", despill off"
        keyed += f", own hues {' '.join(drawn.own_hues)}" if drawn.own_hues else ""
    return f"{keyed}, light {float(light):g}" if light is not None else keyed


def _same_despill(first: dict | None, second: dict | None) -> bool:
    """Whether two drawings took the key's spill off the edge alike: a record kept before it is the default."""
    return screen_insert.Despill.of(first) == screen_insert.Despill.of(second)


def _same_settings(first: dict | None, second: dict | None) -> bool:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return first is None and second is None
    return all(
        (str(first.get(name)) == str(second.get(name))) if name == "color" else abs(float(first.get(name) or 0.0) - float(second.get(name) or 0.0)) < 1e-4
        for name in ("color", "tolerance", "feather")
    )


def unchanged(where: Placed, measure: str, value: float, earlier: list[dict] | None, *, inside: float | None = None) -> dict | None:
    """The newest earlier drawing of *where* that differs from it in its key, its light or its edge despill alone and
    measured *measure* (a key of :func:`reports`' ``measures``) at *value* to :data:`UNCHANGED_DECIMALS` decimals:
    ``{render_id, key_settings, light, despill?, value}``; ``None`` when none.

    The setting it changed is then not what leaves the finding. A drawing at the same key, light and despill is no
    evidence, nor one whose other fields differ (:attr:`Placed.drawn`, when both are known). An older record
    holds the residual key inside the quad alone: compared with *inside*; a still older one was drawn at the
    default despill.
    """
    for record in earlier or []:
        if not isinstance(record, dict):
            continue
        then = (record.get("measures") or {}).get(measure)
        if then is None and measure == "residual_key" and inside:
            then, value = record.get("inside"), inside
        if then is None or round(float(then), UNCHANGED_DECIMALS) != round(float(value), UNCHANGED_DECIMALS):
            continue
        if where.drawn is not None and record.get("rest") is not None and record["rest"] != where.drawn:
            continue
        settings, light, despill = record.get("key_settings"), record.get("light"), record.get("despill")
        same_light = light is None or where.light is None or abs(float(light) - float(where.light)) < 1e-4
        same_despill = where.despill is None or _same_despill(despill, where.despill)
        if _same_settings(settings, where.key_settings) and same_light and same_despill:
            continue
        found = {"render_id": record.get("render_id"), "key_settings": settings, "light": light, "value": float(then)}
        return {**found, "despill": despill} if isinstance(despill, dict) else found
    return None


def _not_a_setting(cause: str, detail: str, still: dict, where: Placed, *, named: str | None = None) -> str:
    """``not a setting: <cause> - <detail>``: the words of a finding *still* (:func:`unchanged`) shows no key, feather or
    light setting moves, *cause* one of :data:`NOT_A_SETTING_CAUSES` - *named* after it in brackets when
    given (the colour it is) -, with the two drawings it was the same on."""
    if cause not in NOT_A_SETTING_CAUSES:
        raise ValueError(f"not a cause: {cause}")
    # The despill is said only where the two drawings differ in it.
    now, then = (None, None) if _same_despill(where.despill, still.get("despill")) else (where.despill, still.get("despill") or {})
    said = f"{cause} ({named})" if named else cause
    return (
        f"not a setting: {said} - {detail} (the same {float(still['value']):.3f} at "
        f"{_settings_text(where.key_settings, where.light, now)} as at "
        f"{_settings_text(still.get('key_settings'), still.get('light'), then)}, render `{still.get('render_id')}`)"
    )


def _residual_items(where: dict | None) -> list[tuple[float, str, str]]:
    return sorted(
        ((float(share), region, side) for region, sides in (where or {}).items() for side, share in (sides or {}).items()),
        reverse=True,
    )


def residual_where_text(where: dict | None, *, most: int = 4) -> str:
    """``1.18% in the margin past the bezel on its right, 0.02% inside the quad along its top``: where a residual key
    sits (``FrameAudit.residual_where``), the most first; empty for nothing known."""
    items = _residual_items(where)
    return ", ".join(
        f"{share * 100:.2f}% {_RESIDUAL_PLACES.get(region, region + ' {side}').format(side=side)}" for share, region, side in items[:most]
    )


def _residual_action(where: dict | None, *, still: dict | None = None, now: Placed | None = None) -> dict:
    """The one action of a residual key: where most of it sits decides it (:data:`_RESIDUAL_ACTIONS`); the
    surface running past the quad when nothing says where.

    *still* (:func:`unchanged`) is an earlier drawing at another key or light that left the same residual
    #(6279), *now* the insert: then ``not a setting:`` and its cause, with the two drawings' settings.
    """
    regions = [region for _share, region, _side in _residual_items(where) if region in _RESIDUAL_ACTIONS] or ["edge"]
    kind, cause, what, fix = _RESIDUAL_ACTIONS[regions[0]]
    if still is not None and now is not None:
        return action(kind, _not_a_setting(cause, f"{what} - {fix}", still, now))
    return action(kind, f"{what}: {fix}")


def _quad_text(quad: Any) -> str:
    try:
        return "[" + ", ".join(f"[{float(x):.3f}, {float(y):.3f}]" for x, y in quad) + "]"
    except (TypeError, ValueError):
        return str(quad)


def _colour_report(frames: dict[int, dict], light: float | None) -> dict | None:
    """An insert's colours over the frames drawn, at its *light*: the mean luminance move, the worst black lift,
    hue turn and cast (with its hue's name), the saturation farthest from its source's, the lowest contrast, and how
    many frames failed; ``None`` with none measured."""
    measured = [frames[n] for n in sorted(frames) if frames[n].get("black_lift") is not None]
    if not measured:
        return None

    def values(name: str) -> list[float]:
        return [float(a[name]) for a in measured if a.get(name) is not None]

    casts = [a for a in measured if a.get("cast") is not None]
    worst_cast = max(casts, key=lambda a: float(a["cast"])) if casts else None
    saturations, contrasts, turns = values("saturation"), values("contrast"), values("hue_rotation")
    return {
        "light": light,
        "luma_shift": round(sum(values("luma_shift")) / len(measured), 4),
        "black_lift": round(max(values("black_lift")), 4),
        "hue_rotation": round(max(turns), 2) if turns else None,
        "cast": round(float(worst_cast["cast"]), 2) if worst_cast else None,
        "cast_hue": worst_cast.get("cast_hue") if worst_cast else None,
        "saturation": round(max(saturations, key=lambda v: abs(v - 1.0)), 3) if saturations else None,
        "contrast": round(min(contrasts), 3) if contrasts else None,
        "frames_failed": sum(1 for a in measured if colour_bad(a) > 1.0),
    }


def _edge_spill_report(frames: dict[int, dict], despill: dict | None) -> dict | None:
    """What the edge despill corrected over an insert's frames drawn (``FrameAudit.edge_spill``): ``{edge,
    width_px, strength, max, mean, frames, frames_read}`` - its settings, the most and the mean share of the
    foreground's edge it corrected, the frames it corrected some of, and those it read; ``None`` with no edge read and
    the despill on."""
    shares = [float(frames[n]["edge_spill"]) for n in sorted(frames) if frames[n].get("edge_spill") is not None]
    drawn = screen_insert.Despill.of(despill).as_dict() if despill is not None else None
    if not shares:
        return {**drawn, "max": None, "mean": None, "frames": 0, "frames_read": 0} if drawn and not drawn["edge"] else None
    return {
        **(drawn or {}),
        "max": round(max(shares), 4),
        "mean": round(sum(shares) / len(shares), 4),
        "frames": sum(1 for share in shares if share > 0.0),
        "frames_read": len(shares),
    }


def _band_report(frames: dict[int, dict]) -> dict | None:
    """The bands a contain fit left inside an insert's quad over the frames drawn: ``{share, filled_with,
    colour}`` - the largest share, and what filled them on most frames with its colour on the last of them; ``None``
    with no band."""
    banded = [frames[n] for n in sorted(frames) if frames[n].get("band_share")]
    if not banded:
        return None
    fills = [str(a.get("band_fill")) for a in banded]
    most = max(dict.fromkeys(fills), key=fills.count)
    last = next(a for a in reversed(banded) if str(a.get("band_fill")) == most)
    report = {
        "share": round(max(float(a["band_share"]) for a in banded), 4),
        "filled_with": most,
        "colour": last.get("band_colour"),
    }
    # Left to the footage and of a chroma key colour on most of its frames: a surface left unkeyed (``band_key``).
    keyed = [a for a in banded if a.get("band_key")]
    if most == "footage" and 2 * len(keyed) > len(banded):
        names = [str(a["band_key"]) for a in keyed]
        report["key"] = max(dict.fromkeys(names), key=names.count)
        report["key_share"] = round(max(float(a.get("band_key_share") or 0.0) for a in keyed), 4)
    return report


def _measures(report: dict, frames: dict[int, dict]) -> dict[str, float]:
    """The value of each finding a key or light setting is the advice for, as its finding reads it
    (:func:`unchanged`): the worst residual key, the worst colour past its thresholds, the worst halo, the flicker's
    frames. Rounded to :data:`UNCHANGED_DECIMALS`."""
    found: dict[str, float] = {}
    if report.get("residual_key") is not None:
        found["residual_key"] = float(report["residual_key"])
    colours = [colour_bad(audit) for audit in frames.values() if audit.get("black_lift") is not None]
    if colours:
        found["colour"] = max(colours)
    if report.get("halo") is not None:
        found["halo"] = float(report["halo"])
    if report.get("rim") is not None:
        found["rim"] = float(report["rim"])
    # Its matte's reversals alone: the frames not drawn its flicker fact counts too are no setting's.
    found["flicker"] = float(int(report.get("flicker_frames") or 0) - int(report.get("missing_frames") or 0))
    return {name: round(value, UNCHANGED_DECIMALS) for name, value in found.items()}


def _flatness_report(frames: dict[int, dict], saved: Any = None) -> dict | None:
    """What an insert's frames say of its surface's flatness: how far it is seen at an angle on its first frame
    read (a fact: ``angle``, ``angle_words``, and ``seen_at`` in degrees), and its worst confirmed bow with the
    frames that read one (``curved``, ``bow``, ``frames_curved``) and those the insert drawn there covers
    (``frames_covered``: all of them, ``covered_by_insert``); ``None`` with no edge read. An insert the save
    measured (*saved*) says the save's measure instead, on the footage frame (``measured``)."""
    if saved is not None:
        return _saved_flatness(saved)
    shapes = [(n, shape) for n in sorted(frames) if (shape := frame_flatness(frames[n])) is not None]
    if not shapes:
        return None
    _first, seen = shapes[0]
    curved = [(n, shape) for n, shape in shapes if not shape.flat]
    out: dict[str, Any] = {
        "angle": round(seen.angle, 4),
        "angle_words": seen.angle_words(),
        "degrees": seen.degrees,
        "seen_at": seen.seen_at(),
        # Per edge, what its points said on the first frame read: its bow, how much of it was visible, how
        # many of its points lie on one line and how far they stand off one.
        "edges": seen.edges(),
    }
    # The edges mostly covered by what stands in front, and on how many frames: their bow not judged there.
    covered: dict[str, int] = {}
    for _n, shape in shapes:
        for side in shape.covered():
            covered[side] = covered.get(side, 0) + 1
    if covered:
        out["covered"] = covered
    if curved:
        _n, worst = max(curved, key=lambda item: item[1].error)
        out.update({"curved": worst.words(), "bow": round(worst.error, 4), "frames_curved": len(curved), "edges": worst.edges()})
        drawn_over = sum(1 for n, shape in curved if not _curved(shape, frames[n]))
        if drawn_over:
            out["frames_covered"] = drawn_over
            out["covered_by_insert"] = drawn_over == len(curved)
    return out


def _saved_flatness(saved: Any) -> dict:
    """What the save measured of an insert's surface on its footage frame (``insert_bezels.Measured``), as
    :func:`_flatness_report` says it: the one measure its previews and its renders hold to (``measured``)."""
    from app.services.video_studio import insert_bezels

    shape = saved.bezel.flatness
    out: dict[str, Any] = {
        "angle": round(shape.angle, 4),
        "angle_words": shape.angle_words(),
        "degrees": shape.degrees,
        "seen_at": shape.seen_at(),
        "edges": shape.edges(),
        "measured": f"at save, on {saved.where}",
    }
    if shape.covered():
        out["covered"] = dict.fromkeys(shape.covered(), 1)
    if not shape.flat:
        out.update({"curved": shape.words(), "bow": round(shape.error, 4)})
        if insert_bezels.bow_covered(saved):
            out["covered_by_insert"] = True
    return out


def _foreground_report(frames: dict[int, dict]) -> dict | None:
    """What stands in front of an insert, frame by frame: ``{max, frames, skipped, by_frame, confidence?}`` -
    its largest share of the quad, the frames it covers any of, those whose colours were not compared, its share on
    each of those frames and how sure its mask is (:func:`_confidence_report`); ``None`` with nothing in front
    on any frame."""
    covered = {n: float(frames[n]["foreground_share"]) for n in sorted(frames) if frames[n].get("foreground_share")}
    if not covered:
        return None
    report = {
        "max": round(max(covered.values()), 4),
        "frames": len(covered),
        "skipped": sum(1 for audit in frames.values() if audit.get("colour_skipped")),
        "by_frame": {str(n): round(share, 3) for n, share in covered.items()},
    }
    if (confidence := _confidence_report(frames)) is not None:
        report["confidence"] = confidence
    return report


def _confidence_words(sure: Any) -> str:
    """How sure the mask of what stands in front is, as an insert's line says it: its lowest, when, its mean
    and - under ``screen_insert.LOW_FRONT_CONFIDENCE`` - what that means; ``""`` with nothing read."""
    if not isinstance(sure, dict):
        return ""
    low = int(sure.get("low") or 0)
    at = sure.get("lowest_at")
    words = (
        f"; its mask {float(sure.get('lowest') or 0.0):.0%} sure at its lowest"
        f"{f' (t={clock(float(at))})' if at is not None else ''}, {float(sure.get('mean') or 0.0):.0%} on average over "
        f"{int(sure.get('frames') or 0)} frame(s)"
    )
    if low:
        words += (
            f" - under {screen_insert.LOW_FRONT_CONFIDENCE:.0%} on {low} frame(s): told from the surface mostly by its "
            "link to what crosses the surface's edge, not by its own colour (dark on dark)"
        )
    return words


def _confidence_report(frames: dict[int, dict]) -> dict | None:
    """How sure the mask of what stands in front of an insert is over its frames (``FrameAudit.front_confidence``):
    ``{lowest, lowest_frame, mean, frames, low, ghost}`` - its lowest and the frame of it, its mean, the frames it was
    read on, those under ``screen_insert.LOW_FRONT_CONFIDENCE`` and, of them, those where it covers more than
    ``screen_insert.GHOST_MIN_FOREGROUND`` of the quad (``FrameAudit.ghosted``); ``None`` with nothing read."""
    read = {
        n: float(frames[n]["front_confidence"]) for n in sorted(frames) if frames[n].get("front_confidence") is not None
    }
    if not read:
        return None
    lowest = min(read, key=lambda n: read[n])
    return {
        "lowest": round(read[lowest], 4),
        "lowest_frame": lowest,
        "mean": round(sum(read.values()) / len(read), 4),
        "frames": len(read),
        "low": sum(1 for value in read.values() if value < screen_insert.LOW_FRONT_CONFIDENCE),
        "ghost": sum(1 for n in read if screen_insert.FrameAudit.from_dict(frames[n]).ghosted),
    }


def _spill_report(frames: dict[int, dict], rate: float, offset: float) -> dict | None:
    """The key's hue past a keyed insert's quad over its frames (``FrameAudit.key_spill``): ``{max, frames, frame,
    at, box}`` - its largest share of the frame, the frames it shows past ``screen_insert.SPILL_MIN_SHARE`` on, the
    worst of them, when, and its largest region's box (0-1 of the frame); ``None`` with none."""
    spilled = [n for n in sorted(frames) if (frames[n].get("key_spill") or 0.0) > screen_insert.SPILL_MIN_SHARE]
    if not spilled:
        return None
    worst = max(spilled, key=lambda n: float(frames[n]["key_spill"]))
    return {
        "max": round(float(frames[worst]["key_spill"]), 5),
        "frames": len(spilled),
        "frame": worst,
        "at": round(max(0.0, worst / rate - offset), 3),
        "box": frames[worst].get("key_spill_box"),
    }


def _size_of(frames: dict[int, dict]) -> list[int] | None:
    """The size an insert's frames were composited and measured at: the largest, ``[w, h]``; ``None`` unknown."""
    sizes = [tuple(a["size"]) for a in frames.values() if isinstance(a.get("size"), list | tuple) and len(a["size"]) == 2]
    return [int(v) for v in max(sizes, key=lambda s: s[0] * s[1])] if sizes else None


#: How an insert's frame-by-frame gates covered its frames (dev:video-render-wide-insert-gate): every frame its layer is
#: drawn on audited, or only some of them (a frame read back without its audit, a scene range).
EVERY_FRAME, SAMPLED = "every_frame", "sampled"
#: The failing frames an insert's coverage keeps by number with their gates (``over_frames``): what the sweep's table
#: says beside each tile; past it, counted only - the report stays compact.
COVERAGE_MAX_FRAMES = 240


def failing_frames(frames: dict[int, dict], *, legible: bool = False) -> dict[int, list[str]]:
    """``output frame -> the gates it fails on its own`` of an insert's audited *frames*: the frame-by-frame gates
    (:func:`gate_frames`, the flicker left out - a reversal is two frames' doing) and what stands in front (``through``,
    ``ghost``, ``covered``, ``key_spill``). What :func:`coverage` and the sweep's table read."""
    out: dict[int, list[str]] = {}
    for gate, failing in gate_frames(frames).items():
        if gate == "flicker":
            continue
        for n in failing:
            out.setdefault(n, []).append(gate)
    for n, audit in frames.items():
        read = screen_insert.FrameAudit.from_dict(audit)
        for gate, bad in (
            ("through", read.through_foreground),
            ("ghost", read.ghosted),
            ("covered", read.covers_foreground or read.draws_edge_line),
            ("key_spill", read.spills_key),
            ("matte_halo", read.matte_haloed),
            ("matte_lag", read.matte_lags),
        ):
            if bad:
                out.setdefault(n, []).append(gate)
        # A legible insert hidden past its share on this frame (``insert_occluded``): the sweep frames it too.
        if legible and read.foreground_share is not None and read.foreground_share > screen_insert.OCCLUDED_SHARE:
            out.setdefault(n, []).append("occluded")
    return {n: sorted(set(gates)) for n, gates in sorted(out.items())}


def coverage(where: Placed, frames: dict[int, dict], *, rate: float, offset: float = 0.0, placements: dict[int, Any] | None = None) -> dict:
    """What an insert's gates read of it (dev:video-render-wide-insert-gate): ``{mode, checked, of, over, worst_frame?,
    worst_at?, worst_gates?, run?, run_at?}`` - *mode* :data:`EVERY_FRAME` when every frame that is its scene's its layer
    is drawn on (:meth:`Placed.judged_frames`) was audited, else :data:`SAMPLED`; *over* the frames failing a gate on
    their own (:func:`failing_frames`), the worst the one failing the most gates (the earliest of equal ones) and the
    longest run of consecutive failing frames, in the MP4's seconds."""
    judged = where.judged_frames(placements) if where.expected else sorted(frames)
    of = len(judged) or len(frames)
    checked = len([n for n in judged if n in frames]) if where.expected else len(frames)
    failing = failing_frames(frames, legible=bool(getattr(where, "legible", False)))
    out: dict[str, Any] = {"mode": EVERY_FRAME if checked >= of else SAMPLED, "checked": checked, "of": of, "over": len(failing)}
    if failing:
        worst = min(failing, key=lambda n: (-len(failing[n]), n))
        run = longest_run(list(failing)) or (worst, worst)
        out.update(
            {
                "worst_frame": worst,
                "worst_at": round(max(0.0, worst / rate - offset), 3),
                "worst_gates": failing[worst],
                "run": [run[0], run[1]],
                "run_at": [round(max(0.0, n / rate - offset), 3) for n in run],
                "over_frames": {str(n): gates for n, gates in list(failing.items())[:COVERAGE_MAX_FRAMES]},
            }
        )
    return out


#: The finding each gate a frame fails on its own (:func:`failing_frames`) feeds: the gate the sweep frames a tile with
#: and the finding the render reports are one name (:func:`reconcile_coverage`).
FINDING_OF_GATE = {
    **{gate: kind for kind, gate in GATE_OF.items()},
    "through": INSERT_THROUGH_FOREGROUND,
    "ghost": INSERT_GHOST,
    "covered": INSERT_FOREGROUND,
    "key_spill": KEY_SPILL_OUTSIDE_QUAD,
    "matte_halo": INSERT_MATTE_HALO,
    "matte_lag": INSERT_MATTE_LAG,
    "occluded": INSERT_OCCLUDED,
}


def reconcile_coverage(reports: list[dict], findings: list[Finding]) -> list[str]:
    """One source of truth between a render's *findings* and the frames its inserts' *reports* name failing a gate on
    their own (``coverage.over_frames``, what ``get_video_insert_sweep`` frames): each report's coverage gets
    ``under_tolerance`` - ``{gate: frames}`` of the gates its frames fail with no finding of that gate on the insert
    (under its tolerance: :data:`MIN_FAILING_FRAMES`, ``surface_refine.PERSIST_FRAMES`` consecutive frames), which the
    sweep frames as advisories, never as failures - and ``findings`` - the gates it has a finding of. The facts returned
    say each insert's advisory frames, and a finding of a frame gate whose frames no gate failed on its own (a
    disagreement between the two readings, the checks' defect, never silent)."""
    by_insert: dict[str, set[str]] = {}
    for finding in findings:
        key = (finding.data or {}).get("insert")
        if key:
            by_insert.setdefault(str(key), set()).add(finding.kind)
    out: list[str] = []
    for report in reports:
        read = report.get("coverage")
        if not isinstance(read, dict):
            continue
        key, label = str(report.get("key") or ""), str(report.get("label") or report.get("key") or "")
        found = by_insert.get(key, set())
        counted: dict[str, int] = {}
        for gates in (read.get("over_frames") or {}).values():
            for gate in gates:
                counted[str(gate)] = counted.get(str(gate), 0) + 1
        under = {gate: count for gate, count in sorted(counted.items()) if FINDING_OF_GATE.get(gate, gate) not in found}
        held = sorted(gate for gate in counted if FINDING_OF_GATE.get(gate, gate) in found)
        if under:
            read["under_tolerance"] = under
            said = ", ".join(f"{gate} on {count} frame{'s' if count != 1 else ''}" for gate, count in under.items())
            out.append(
                f"{label}: frames fail a gate on their own under its tolerance ({said}; a gate fails an insert on "
                f"{MIN_FAILING_FRAMES} frames in a row) - advisory tiles in its sweep, not findings."
            )
        if held:
            read["findings"] = held
        if read.get("mode") != EVERY_FRAME:
            continue
        for kind in sorted(found):
            gate = GATE_OF.get(kind)
            if gate is not None and gate != "flicker" and gate not in counted and int(read.get("over") or 0) < COVERAGE_MAX_FRAMES:
                out.append(
                    f"{label}: its {kind} finding names frames no frame of its coverage fails on its own ({gate}) - the "
                    "render's two readings of the insert disagree; the finding stands."
                )
    return out


def coverage_words(report: dict) -> str | None:
    """``its gates read 240 of 240 frames (every frame); 5 over a gate, the worst at 0:05.00 (through), 5 in a row`` - an
    insert report's :func:`coverage` as its answer line says it; ``None`` without one."""
    read = report.get("coverage") if isinstance(report.get("coverage"), dict) else None
    if read is None:
        return None
    mode = "every frame" if read.get("mode") == EVERY_FRAME else "sampled"
    text = f"its gates read {int(read.get('checked') or 0)} of {int(read.get('of') or 0)} frames ({mode})"
    if read.get("over"):
        run = read.get("run") or [0, 0]
        text += (
            f"; {int(read['over'])} over a gate, the worst at {clock(read.get('worst_at'))} "
            f"({', '.join(read.get('worst_gates') or [])}), {int(run[1]) - int(run[0]) + 1} in a row at its longest"
        )
    return text


def reports(
    audits: dict[str, dict],
    placed: dict[str, Placed],
    *,
    fps: float,
    offset: float = 0.0,
    presence: dict[str, dict] | None = None,
    measured: dict[str, Any] | None = None,
) -> list[dict]:
    """What each screen insert's compositing measured, one entry per insert drawn: the ``checks.inserts``.

    ``{key, label, scene_id, frames, refined, seed_error, alignment_max, residual_key, residual_where?,
    residual_crop?, flicker_frames, sharpness, halo, colour, measured_at, cropped_at, crops: [{frame, at, word, box,
    path}]}`` - the refined quad of the first frame drawn, the mean seed error over the frames where the bezel was
    found and the worst, the worst residual key with where it sits on that frame
    (``FrameAudit.residual_where``) and the crop of the side holding most of it on the worst frame kept (``{frame,
    at, side, path}``), the flicker frames, the lowest sharpness, the worst halo, its colours at its light
    (:func:`_colour_report`), the size its frames were measured at and the size of the frame its crops were cut from
    (``[w, h]``), and the crops kept of its region at its first, middle and last frame, each with its box
    (0-1 of the frame: what ``save_video_frame``'s ``region`` cuts at any other moment; ``path`` until the render
    stores them as files, ``file_id`` then). The bands a contain fit left inside its quad and what filled them
    (``contain_band: {share, filled_with, colour}``), and, keyed, its ``fit``, its key as set (``key_settings``) and
    the residual key inside its quad on the worst frame (``residual_inside``) - what the next drawing is held to.
    The bezel side by side on the frame worst off it (``alignment_sides``), how far its surface is seen at an
    angle and its worst confirmed bow (``flatness``, :func:`_flatness_report`), the source's own key hue
    (``source_key_hue``), what stands in front frame by frame (``foreground``, :func:`_foreground_report`), the
    halo's edge length (``halo_edge``), and what the next drawing holds its findings to: the digest of every field
    but its key, light and despill (``drawn_rest``) and each finding's value (``measures``, :func:`_measures`).
    What its edge despill corrected of the foreground's edge, frame by frame (``edge_spill``,
    :func:`_edge_spill_report`), and the worst stretch of its halo at 100 % (``halo_crop: {frame, at, path}``).
    Its worst halo frame's edge told apart (``halo_parts: {frame, at, edge_px, spill, own, clamped, hue?}``,
    :func:`_halo_parts`). What it measured on its scene's joins with the scenes beside it (``joins: {frames,
    residual_key, read?}``, ``read`` the presence gate's join spans in the MP4's seconds, ``{present,
    missing}``, kept for debugging) and the frames that are its scene's the page draws it on that its layer was not
    drawn on (``missing_frames``: those of :func:`missing_frames` the presence gate read it missing on,
    ``presence.undrawn``) - its ``flicker_frames`` counting the frames its flicker check fails on those that are
    its scene's, these included (:func:`owned_flicker`). What the presence gate read of it off the encoded frames
    (*presence*, :attr:`Presence.by_key`): ``presence: {frames, missing, unjudged, joins, joins_missing,
    first_missing?}``. An insert the save measured (*measured*, :func:`findings`) says the save's flatness, and
    its verdicts take it. Its worst grey rim (``rim``), that frame's edge and the share of the rim its light
    made grey (``rim_parts: {frame, at, edge_px, light}``), and the rim's worst stretch at 100 % (``rim_crop: {frame,
    at, path}``). Its quad's worst jump from one frame to the next beyond the motion around it (``jitter_max``, a
    share of the frame's width, :func:`quad_jitter`), between the frames ``jitter_frames`` at ``jitter_at``.
    """
    rate = max(float(fps), 1.0)
    out: list[dict] = []
    for key, where in placed.items():
        saved = saved_measure(measured, key)
        part = audits.get(key) or {}
        frames: dict[int, dict] = {int(n): audit for n, audit in (part.get("frames") or {}).items()}
        crops: dict[int, str] = {int(n): path for n, path in (part.get("crops") or {}).items()}
        boxes: dict[int, list] = {int(n): box for n, box in (part.get("crop_boxes") or {}).items()}
        read = (presence or {}).get(key)
        if not frames and not crops and read is None:
            continue
        ordered = sorted(frames)
        errors = [float(frames[n]["alignment"]) for n in ordered if frames[n].get("alignment") is not None]
        measured = {
            name: [float(frames[n][name]) for n in ordered if frames[n].get(name) is not None]
            for name in ("residual_key", "sharpness", "halo", "rim")
        }
        taken = sorted(crops)
        words = {1: ("only",), 2: ("first", "last"), 3: ("first", "middle", "last")}.get(len(taken)) or tuple(
            str(index) for index in range(len(taken))
        )
        spots = placements_of(part)
        turned = owned_flicker(where, frames, spots)
        # Its layer not drawn, as the presence gate read it off the encoded frames: never the page log alone.
        undrawn = int(read.get("undrawn") or 0) if isinstance(read, dict) else 0
        report = {
            "key": key,
            "label": where.label,
            "scene_id": where.scene_id,
            # The settings the author chose it is drawn by: what makes a measurement by design (:func:`classify`).
            "drawn_by": {
                "fit": where.fit,
                "light": where.light,
                "key": where.key_settings,
                **({"despill": where.despill} if where.despill is not None else {}),
            },
            "frames": len(frames),
            # Every frame its gates read, and the worst of them (dev:video-render-wide-insert-gate).
            "coverage": coverage(where, frames, rate=rate, offset=offset, placements=placements_of(part)),
            "refined": next((frames[n]["refined"] for n in ordered if frames[n].get("refined")), None),
            "seed_error": round(sum(errors) / len(errors), 5) if errors else None,
            "alignment_max": round(max(errors), 5) if errors else None,
            "residual_key": round(max(measured["residual_key"]), 5) if measured["residual_key"] else None,
            # The frames its flicker check fails on, on those that are its scene's: its findings' own count.
            "flicker_frames": len(turned) + undrawn,
            "sharpness": round(min(measured["sharpness"]), 4) if measured["sharpness"] else None,
            "halo": round(max(measured["halo"]), 4) if measured["halo"] else None,
            "rim": round(max(measured["rim"]), 4) if measured["rim"] else None,
            "colour": _colour_report(frames, where.light),
            "measured_at": _size_of(frames),
            "cropped_at": list(part["crop_frame"]) if part.get("crop_frame") else None,
            # Where its crops were cut, in the MP4: its scene on screen alone, out of both transitions.
            "cropped_within": [round(max(0.0, at - offset), 3) for at in crop_window(where)],
            "crops": [
                {
                    "frame": n,
                    "at": round(max(0.0, n / rate - offset), 3),
                    "word": words[index],
                    **({"box": boxes[n]} if n in boxes else {}),
                    "path": crops[n],
                }
                for index, n in enumerate(taken)
            ],
        }
        residue = [n for n in ordered if (frames[n].get("residual_key") or 0.0) > 0.0]
        if residue:
            worst = max(residue, key=lambda n: float(frames[n]["residual_key"]))
            if frames[worst].get("residual_where"):
                report["residual_where"] = frames[worst]["residual_where"]
            # Under the gate's share on every frame - or past it on fewer frames than fail it (the
            # persistence of :func:`gate_fails`): said, never a finding, its worst frame kept.
            past = [n for n in residue if residual_past(frames[n])]
            report["residual_advisory"] = not past or not gate_fails(
                "residual_key", past, len(frames), joined=[n for n in past if where.at_join(n)]
            )
            if past and report["residual_advisory"]:
                report["residual_past_frames"] = len(past)
        # The key's colour the layer took out of its margin: the worst frame's share and sides.
        spilled = [n for n in ordered if (frames[n].get("despilled") or 0.0) > 0.0]
        if spilled:
            worst = max(spilled, key=lambda n: float(frames[n]["despilled"]))
            report["despilled"] = {"share": float(frames[worst]["despilled"]), "frames": len(spilled), "sides": frames[worst].get("despilled_where") or {}}
        # The frames past the alignment gate, and those the bezel lock's prior held on the last frames' bezel.
        off = _off_bezel(frames)
        if off:
            report["alignment_frames"] = len(off)
        # The bezel moving from under a static insert's lock: its worst frame, and the bezel found there.
        drifts = [n for n in ordered if frames[n].get("alignment_drift") is not None]
        if drifts:
            worst = max(drifts, key=lambda n: float(frames[n]["alignment_drift"]))
            report["drift_max"] = round(float(frames[worst]["alignment_drift"]), 5)
            report["drift_frame"] = worst
            report["drift_at"] = round(max(0.0, worst / rate - offset), 3)
            if frames[worst].get("drift_sides"):
                report["drift_sides"] = frames[worst]["drift_sides"]
            if frames[worst].get("bezel_found"):
                report["bezel_found"] = frames[worst]["bezel_found"]
        # Its quad's worst jump from one frame to the next, beyond the motion around it (:func:`quad_jitter`).
        if (jumped := quad_jitter(frames)) is not None:
            report["jitter_max"] = round(jumped.share, 5)
            report["jitter_frames"] = [jumped.before, jumped.frame]
            report["jitter_at"] = [round(max(0.0, n / rate - offset), 3) for n in (jumped.before, jumped.frame)]
        held = sum(1 for n in ordered if frames[n].get("held"))
        if held:
            report["held_frames"] = held
        # The bezel readings its findings did not judge (:func:`bezel_readings`): the measure that stands instead.
        for said in ("bezel_stored", "bezel_discarded"):
            if isinstance(part.get(said), dict):
                report[said] = dict(part[said])
        # What the next preview holds this one to: the key as set, and the residual inside its quad.
        if where.key_settings is not None and measured["residual_key"]:
            report["fit"] = where.fit
            report["key_settings"] = where.key_settings
            report["residual_inside"] = inside_share(report.get("residual_where")) if residue else 0.0
        # ... and every other field it is drawn by, and the value of each finding a setting is the advice for;
        # and whether each gate passed - what a later drawing of the same shot and spec is held to.
        report["drawn_rest"] = where.drawn
        # Its layer not drawn on a frame that is its scene's, the encoded frame reading it missing
        # there: counted in its flicker fact, kept apart for debugging only - never said on its own.
        if where.expected and read is not None:
            report["missing_frames"] = undrawn
        report["measures"] = _measures(report, frames)
        report["verdicts"] = verdicts(frames, where, saved, spots)
        # Its scene's joins with the scenes beside it, both transitions and a cut's frames: the residual key its
        # layer left there, and what the presence gate read there off the encoded frames - its own join spans, in the
        # MP4's seconds, kept for debugging: its presence line says what the gate read.
        joined = [n for n in ordered if where.at_join(n)]
        spans = read.get("join_spans") if isinstance(read, dict) and isinstance(read.get("join_spans"), dict) else None
        if joined or spans:
            report["joins"] = {
                "frames": len(joined),
                "residual_key": round(max(float(frames[n].get("residual_key") or 0.0) for n in joined), 5) if joined else 0.0,
            }
            if spans:
                report["joins"]["read"] = {
                    side: [[round(max(0.0, float(a) - offset), 3), round(max(0.0, float(b) - offset), 3)] for a, b in spans.get(side) or []]
                    for side in ("present", "missing")
                }
        if read is not None:
            report["presence"] = dict(read)
        # The bezel side by side on the frame worst off it, and what the edges' points say of the surface.
        aligned = [n for n in ordered if frames[n].get("alignment_sides")]
        if aligned:
            report["alignment_sides"] = frames[max(aligned, key=lambda n: _alignment_of(frames[n]) or 0.0)]["alignment_sides"]
        if (shape := _flatness_report(frames, saved)) is not None:
            report["flatness"] = shape
        # The source's own key hue, left out of the residual key and the halo; what stands in front, left out of the
        # colours and the sharpness, frame by frame, and the frames its colours were not compared on.
        own = [float(frames[n]["source_key_hue"]) for n in ordered if frames[n].get("source_key_hue")]
        if own:
            report["source_key_hue"] = round(max(own), 5)
        if (front := _foreground_report(frames)) is not None:
            if isinstance(front.get("confidence"), dict):
                front["confidence"]["lowest_at"] = round(
                    max(0.0, int(front["confidence"]["lowest_frame"]) / rate - offset), 3
                )
            report["foreground"] = front
        # The worst stretch of the insert drawn over what stands in front, at 100 %.
        over = _worst((part.get("evidence") or {}).get("foreground_crop") or [])
        if over is not None and over.get("path") and int(over["frame"]) in frames:
            n = int(over["frame"])
            report["foreground_crop"] = {"frame": n, "at": round(max(0.0, n / rate - offset), 3), "path": over["path"]}
        # The source's own strokes drawn across what stands in front: its worst share and stretch at 100 %.
        through = [n for n in ordered if (frames[n].get("through") or 0.0) > 0.0]
        if through:
            worst = max(through, key=lambda n: float(frames[n]["through"]))
            report["through"] = {"max": round(float(frames[worst]["through"]), 5), "frame": worst, "frames": len(through)}
        strokes = _worst((part.get("evidence") or {}).get("through_crop") or [])
        if strokes is not None and strokes.get("path") and int(strokes["frame"]) in frames:
            n = int(strokes["frame"])
            report["through_crop"] = {"frame": n, "at": round(max(0.0, n / rate - offset), 3), "path": strokes["path"]}
        # The key's hue past the quad on the whole footage frame.
        if (spill := _spill_report(frames, rate, offset)) is not None:
            report["key_spill"] = spill
        haloed = [n for n in ordered if frames[n].get("halo") is not None]
        if haloed and frames[max(haloed, key=lambda n: float(frames[n]["halo"]))].get("halo_edge"):
            report["halo_edge"] = int(frames[max(haloed, key=lambda n: float(frames[n]["halo"]))]["halo_edge"])
        # Its worst frame's edge told apart: counted, the subject's own colour, where the clamp touched - with
        # nothing counted, the frame that left the most of it out.
        if haloed:
            worst = max(haloed, key=lambda n: (float(frames[n]["halo"]), float(frames[n].get("halo_own") or 0.0)))
            report["halo_parts"] = {"frame": worst, "at": round(max(0.0, worst / rate - offset), 3), **_halo_parts(frames[worst])}
        # The share of the foreground's edge the edge despill corrected, frame by frame: its most and mean,
        # and the frames it corrected anything on.
        if (spill := _edge_spill_report(frames, where.despill)) is not None:
            report["edge_spill"] = spill
        worst_halo = _worst((part.get("evidence") or {}).get("halo_crop") or [])
        if worst_halo is not None and worst_halo.get("path") and int(worst_halo["frame"]) in frames:
            n = int(worst_halo["frame"])
            report["halo_crop"] = {"frame": n, "at": round(max(0.0, n / rate - offset), 3), "path": worst_halo["path"]}
        # Its worst grey rim: the edge it was read on, the share of it its light made grey, its stretch at 100 %.
        rimmed = [n for n in ordered if frames[n].get("rim") is not None]
        if rimmed:
            worst = max(rimmed, key=lambda n: float(frames[n]["rim"]))
            report["rim_parts"] = {
                "frame": worst,
                "at": round(max(0.0, worst / rate - offset), 3),
                "edge_px": int(frames[worst].get("rim_edge") or 0),
                "light": round(float(frames[worst].get("rim_light") or 0.0), 4),
            }
        # The grey band off the keyed surface and on the whole band: facts, never gated - the footage's own
        # surroundings the quad overlaps (a desk, a console) are not the key's residue.
        for name, field in (("rim_surroundings", "rim_surroundings"), ("rim_whole", "rim_whole")):
            values = [float(frames[n][field]) for n in ordered if frames[n].get(field) is not None]
            if values:
                report[name] = round(max(values), 4)
        # The grey the compositing drew off the keyed surface, where the footage had colour: gated as the rim is.
        offs = [float(frames[n]["rim_off"]) for n in ordered if frames[n].get("rim_off") is not None]
        if offs:
            report["rim_off"] = round(max(offs), 4)
        worst_rim = _worst((part.get("evidence") or {}).get("rim_crop") or [])
        if worst_rim is not None and worst_rim.get("path") and int(worst_rim["frame"]) in frames:
            n = int(worst_rim["frame"])
            report["rim_crop"] = {"frame": n, "at": round(max(0.0, n / rate - offset), 3), "path": worst_rim["path"]}
        band = _band_report(frames)
        if band is not None:
            report["contain_band"] = band
        strip = _worst((part.get("evidence") or {}).get("residual_crop") or [])
        if strip is not None and strip.get("path") and int(strip["frame"]) in frames:
            n = int(strip["frame"])
            side = screen_insert.worst_side(frames[n].get("residual_where"))
            report["residual_crop"] = {"frame": n, "at": round(max(0.0, n / rate - offset), 3), "side": side, "path": strip["path"]}
        out.append(report)
    return out


#: The class of an insert measurement. A finding blocks or warns (``render_checks.Finding``); by design is
#: what a setting the author chose - its fit, its light, its key or none - makes the insert show; a fact is a
#: measurement that blocks nothing. Only findings reach a render's verdict.
FINDING, BY_DESIGN, FACT = "finding", "by_design", "fact"
#: The finding each measurement of a report is held to: present on the insert, the measurement is that finding.
_MEASURED_BY = {
    "alignment": INSERT_ALIGNMENT,
    "jitter": INSERT_JITTER,
    "flatness": INSERT_FLATNESS,
    "residual_key": INSERT_RESIDUAL_KEY,
    "flicker": INSERT_FLICKER,
    "sharpness": INSERT_SOFT,
    "halo": INSERT_HALO,
    "rim": INSERT_RIM,
    "colour": INSERT_COLOUR_SHIFT,
    "presence": INSERT_MISSING,
    "measurement": INSERT_MEASUREMENT_LOST,
    "through": INSERT_THROUGH_FOREGROUND,
    "ghost": INSERT_GHOST,
    "key_spill": KEY_SPILL_OUTSIDE_QUAD,
}


@dataclass(frozen=True)
class Measured:
    """One measurement of an insert's report (:func:`reports`), classed."""

    cls: str
    text: str
    #: The settings that make it what it is (:data:`BY_DESIGN`): ``fit contain, no key``.
    because: str | None = None
    #: The finding it is (:data:`FINDING`).
    kind: str | None = None

    def line(self) -> str:
        """``by design (fit contain, no key): contain bands 5.5% of the quad, showing the footage``: the class first."""
        if self.cls == BY_DESIGN:
            return f"by design ({self.because}): {self.text}"
        if self.cls == FINDING:
            return f"finding ({self.kind}): {self.text}"
        return f"fact: {self.text}"


def _keyed_text(key: dict | None) -> str:
    return f"key {key.get('color')}" if isinstance(key, dict) and key.get("color") else "no key"


def classify(report: dict, found: set[str] | frozenset[str] = frozenset()) -> list[Measured]:
    """Every measurement of an insert's *report* (:func:`reports`), in the order an answer says them, each classed:
    a finding when a finding of its kind (*found*, the kinds the render's findings give this insert) holds
    it; by design when it is what its fit, its light or its key makes it show (``drawn_by``: a contain fit's
    bands, the colours at its light, the key colour its key took out of the margin); else a fact.

    A contain fit's bands are its fit's - showing the footage with no key, the bezel's colour or the source's edge
    with one -, never a defect to fix: what sits in them past the gate's share is a finding of its own
    (``insert_residual_key``).
    """
    drawn_by = report.get("drawn_by") if isinstance(report.get("drawn_by"), dict) else {}

    def held(measure: str, text: str, *, because: str | None = None) -> Measured:
        kind = _MEASURED_BY.get(measure)
        if kind is not None and kind in found:
            return Measured(FINDING, text, kind=kind)
        return Measured(BY_DESIGN, text, because=because) if because else Measured(FACT, text)

    from app.services.video_studio.surface_refine import ANGLE_FACT_SHARE

    out: list[Measured] = []
    error = report.get("seed_error")
    discarded = report.get("bezel_discarded") if isinstance(report.get("bezel_discarded"), dict) else None
    present = report.get("presence") if isinstance(report.get("presence"), dict) else None
    lost = present.get("lost") if present is not None and isinstance(present.get("lost"), dict) and present["lost"].get("frame") is not None else None
    if lost is not None:
        # Its measurement lost: what it read is of another region than it is drawn on - its presence, its bezel and
        # the frames it says not drawn are never said as facts.
        out.append(held("measurement", f"measurement lost: {lost_words(lost)}"))
    elif error is not None:
        sides = report.get("alignment_sides") if isinstance(report.get("alignment_sides"), dict) else {}
        by_side = ", ".join(f"{side} {float(share) * 100:.2f}%" for side, share in sides.items())
        out.append(
            held(
                "alignment",
                f"bezel found, the quad {error * 100:.2f}% of the frame's width off it (mean; worst "
                f"{float(report.get('alignment_max') or 0.0) * 100:.2f}%{f'; by side on that frame: {by_side}' if by_side else ''})",
            )
        )
    elif not report.get("bezel_stored") and discarded is None:
        # A reading set aside is no bezel missing: the measure that stands is said (``bezel_readings``).
        out.append(Measured(FACT, "no bezel found under the quad: drawn where the quad is"))
    if discarded is not None:
        count = int(discarded.get("frames") or 0)
        out.append(
            Measured(
                FACT,
                f"bezel reading set aside on {count} frame{'s' if count != 1 else ''} of its scene's joins: drawn smaller "
                "than the footage frame the save measured, and off its measure - the save's stands",
            )
        )
    shape = report.get("flatness") if isinstance(report.get("flatness"), dict) else None
    if shape:
        # Seen at an angle is the perspective warp's to draw: by design, never a fault; a confirmed bow is one.
        # Said in degrees; a report older than them by its opposite sides.
        seen_at = shape.get("seen_at") if "seen_at" in shape else (
            f"seen at an angle: {shape.get('angle_words')}" if float(shape.get("angle") or 0.0) > ANGLE_FACT_SHARE else None
        )
        if seen_at:
            out.append(
                Measured(
                    BY_DESIGN,
                    f"{seen_at}, which the perspective warp draws exactly",
                    because="a planar surface in perspective",
                )
            )
        if shape.get("curved"):
            on = shape.get("measured") or f"on {int(shape.get('frames_curved') or 0)} frame(s)"
            if shape.get("covered_by_insert"):
                # A bow the insert drawn on it covers: on its bezel, no key left past it - nothing of it shows.
                out.append(
                    Measured(FACT, f"surface curved {on}: {shape['curved']}, which the insert drawn on its bezel covers - no key left past it")
                )
            else:
                out.append(held("flatness", f"surface curved {on}: {shape['curved']}"))
        covered = shape.get("covered") if isinstance(shape.get("covered"), dict) else {}
        for side, count in covered.items():
            # An edge mostly behind what stands in front: its bow is not judged there - a fact.
            out.append(Measured(FACT, f"{side} edge mostly covered by what stands in front on {int(count)} frame(s): its bow not judged there"))
    if report.get("held_frames"):
        # The bezel lock's prior: an inner edge the refiner caught, the lock of the frames around kept.
        out.append(
            Measured(FACT, f"bezel lock held on the frames around it on {int(report['held_frames'])} frame(s) where an inner edge was caught")
        )
    if report.get("drift_max") is not None:
        # The bezel read on every frame against the lock: how far it moved at most, and when.
        sides = report.get("drift_sides") if isinstance(report.get("drift_sides"), dict) else {}
        by_side = ", ".join(f"{side} {float(share) * 100:.2f}%" for side, share in sides.items())
        when = f" at {clock(float(report['drift_at']))}" if report.get("drift_at") is not None else ""
        out.append(
            held(
                "alignment",
                f"bezel read on every frame against the lock: it moves at most {float(report['drift_max']) * 100:.2f}% of the "
                f"frame's width from under the quad{when}{f' (by side: {by_side})' if by_side else ''}",
            )
        )
    if report.get("alignment_frames"):
        out.append(held("alignment", f"{int(report['alignment_frames'])} frame(s) past the alignment gate"))
    if report.get("jitter_max") is not None:
        # Its quad held to the frames around it (:func:`quad_jitter`): what the mean alignment averages away.
        pair = report.get("jitter_at") if isinstance(report.get("jitter_at"), list) and len(report["jitter_at"]) == 2 else None
        between = f", between {float(pair[0]):.3f} s and {float(pair[1]):.3f} s" if pair else ""
        out.append(
            held(
                "jitter",
                f"quad jitter at most {float(report['jitter_max']) * 100:.2f}% of the frame's width from one frame to the "
                f"next, beyond the motion around it{between} (the gate: {JITTER_MAX_SHARE * 100:.1f}%)",
            )
        )
    if report.get("residual_key") is not None:
        strip = report.get("residual_crop") if isinstance(report.get("residual_crop"), dict) else {}
        said = [
            text
            for text in (
                residual_where_text(report.get("residual_where")),
                f"its {strip.get('side')} side at 100%: file `{strip['file_id']}`" if strip.get("file_id") else "",
            )
            if text
        ]
        shown = f" ({'; '.join(said)})" if said else ""
        advisory = ""
        if report.get("residual_advisory"):
            past = int(report.get("residual_past_frames") or 0)
            advisory = (
                f" - advisory, past the gate's share on {past} frame{'s' if past != 1 else ''} only, under the "
                f"{MIN_FAILING_FRAMES} it fails on: nothing to fix"
                if past
                else " - advisory, under the gate's share: nothing to fix"
            )
        out.append(held("residual_key", f"residual key {float(report['residual_key']) * 100:.2f}% of the quad{shown}{advisory}"))
    if report.get("source_key_hue"):
        # The key's hue inside the drawn source is what it shows: by design, never the key left.
        out.append(
            Measured(
                BY_DESIGN,
                f"the source's own key hue on {float(report['source_key_hue']) * 100:.2f}% of the quad: what it shows, left out "
                "of the residual key and the halo",
                because="its source's own colours",
            )
        )
    spill = report.get("key_spill") if isinstance(report.get("key_spill"), dict) else None
    if spill:
        # The key's hue past the quad on the whole footage frame: what no margin of the layer reaches.
        box = spill.get("box")
        around = f", around {_box_words(box)}" if isinstance(box, list | tuple) and len(box) == 4 else ""
        out.append(
            held(
                "key_spill",
                f"its key colour past the quad on {float(spill.get('max') or 0.0) * 100:.1f}% of the frame at most "
                f"({int(spill.get('frames') or 0)} frame(s){around})",
            )
        )
    despilled = report.get("despilled") if isinstance(report.get("despilled"), dict) else None
    if despilled:
        sides = ", ".join(f"{side} {float(share) * 100:.2f}%" for side, share in (despilled.get("sides") or {}).items())
        out.append(
            Measured(
                BY_DESIGN,
                f"the footage's key colour taken out of the margin past the quad on {float(despilled['share']) * 100:.2f}% "
                f"of its area ({sides or 'its sides'}; {int(despilled.get('frames') or 0)} frame(s)): despilled",
                because=_keyed_text(drawn_by.get("key")) if isinstance(drawn_by.get("key"), dict) else "its key",
            )
        )
    # The bands a contain fit left inside the quad and what filled them: its fit's, never a defect - but bands left to
    # a footage of a chroma key colour, which ``insert_band_key_colour`` fails.
    band = report.get("contain_band") if isinstance(report.get("contain_band"), dict) else None
    if banded := ("" if band is not None and band.get("key") else band_fill_text(band)):
        out.append(Measured(BY_DESIGN, banded, because=f"fit {drawn_by.get('fit') or 'contain'}, {_keyed_text(drawn_by.get('key'))}"))
    # The frames its flicker check fails on, on those that are its scene's: its layer not drawn there counted in
    # it, never said apart - the note of a frame not drawn is never said beside a flicker of 0.
    out.append(held("flicker", f"flicker {int(report.get('flicker_frames') or 0)} frame(s)"))
    # Its measurement lost: its joins and its presence are of another region than it is drawn on.
    joins = report.get("joins") if isinstance(report.get("joins"), dict) and lost is None else None
    if lost is not None:
        present = None
    if joins and int(joins.get("frames") or 0):
        # Both of its scene's transitions and a cut's frames: the residual key its layer left there - what the
        # encoded frames showed there is its presence line's.
        out.append(
            held(
                "residual_key",
                f"on its scene's joins with the scenes beside it: {int(joins['frames'])} frame(s) measured, residual key "
                f"{float(joins.get('residual_key') or 0.0) * 100:.2f}% of the quad at most",
            )
        )
    if present is not None:
        # Its source read off the encoded frames it shows whole on, its scene's last painted frame included.
        lost = int(present.get("missing") or 0)
        unjudged = int(present.get("unjudged") or 0)
        aside = f"; {unjudged} not judged (covered, or no source frame read)" if unjudged else ""
        # A transition's frames, read unblended, said as a fact: what the blend leaves of the insert there.
        if int(present.get("blended") or 0):
            aside += (
                f"; {int(present['blended'])} of them blended with the scene beside it, read unblended (the insert at "
                f"alpha {float(present.get('least_alpha') or 0.0):.2f} at least)"
            )
        if int(present.get("blended_unread") or 0):
            aside += f"; {int(present['blended_unread'])} blended frame(s) not read (no unblending kept for them)"
        if int(present.get("faint") or 0):
            aside += f"; {int(present['faint'])} frame(s) of a transition show under {PRESENCE_MIN_SHOWN:.0%} of it, not judged"
        if int(present.get("faded") or 0):
            # The video's fade from or to black hides most of the whole frame there, the insert with it.
            aside += f"; {int(present['faded'])} frame(s) the video's fade leaves under {PRESENCE_MIN_SHOWN:.0%} of, not judged"
        if int(present.get("covered") or 0):
            # What its scene draws over the quad is left out of the read: too little left to judge on these.
            aside += (
                f"; {int(present['covered'])} frame(s) with what its scene draws over it (its text, chips, subtitles) on "
                f"more than {1.0 - PRESENCE_MIN_UNCOVERED:.0%} of its quad, not judged"
            )
        if isinstance(present.get("disagrees"), dict):
            # Another drawing of the same join read it the other way (:func:`agree_on_joins`): said, never a finding.
            other = present["disagrees"]
            there = "present" if other.get("here") == "missing" else "missing"
            aside += (
                f"; {other.get('stage') or 'preview'} {other.get('id')} of the same join read it {there} at "
                f"t={clock(float(other.get('at') or 0.0))}: the two disagree on a measure at its threshold there"
            )
        out.append(
            held(
                "presence",
                f"its source missing from {lost} of {int(present.get('frames') or 0)} encoded frame(s) it shows whole on, the "
                f"first at t={clock(float(present.get('first_missing') or 0.0))}{aside}"
                if lost
                else f"its source present on all {int(present.get('frames') or 0)} encoded frame(s) it shows whole on{aside}",
            )
        )
        # A transition or a fade dims or blurs the whole frame, the insert with it: its quad under the floor
        # there, judged against the frame around it, is the transition's.
        if int(present.get("recognised") or 0):
            # A dark source of dim text reads under the floor on its own scene's frames: its structure is there.
            out.append(
                Measured(
                    FACT,
                    f"{int(present['recognised'])} encoded frame(s) under the presence floor ({FLAT_VAR_MIN:g}), its source "
                    f"recognised by its structure there (SSIM over {PRESENCE_SSIM_MIN:g}): present",
                )
            )
        for dimmed in present.get("dimmed") or []:
            frames, whole = int(dimmed.get("frames") or 0), int(dimmed.get("whole_frame") or 0)
            known = int(dimmed.get("recognised") or 0)
            judged = frames - known
            how = (
                "with the whole frame"
                if whole == judged
                else f"{whole} with the whole frame, the rest with the detail of the frame around them"
                if whole
                else "with the detail of the frame around them"
            )
            if known:
                by_structure = f"{known} by its source's structure there (SSIM over {PRESENCE_SSIM_MIN:g})"
                how = by_structure if not judged else f"{by_structure}, {judged} {how}"
            out.append(
                Measured(
                    FACT,
                    f"{frames} encoded frame(s) under the presence floor ({FLAT_VAR_MIN:g}) {how}, inside the "
                    f"{dimmed.get('window')}: the transition's, its source judged present",
                )
            )
    if report.get("sharpness") is not None:
        out.append(held("sharpness", f"sharpness {float(report['sharpness']):.2f} of the source"))
    # What the edge despill took off the foreground's edge: its setting's, never a defect.
    spill = report.get("edge_spill") if isinstance(report.get("edge_spill"), dict) else {}
    despill = screen_insert.Despill.of(drawn_by["despill"]) if isinstance(drawn_by.get("despill"), dict) else None
    if despill is not None and not despill.edge:
        out.append(Measured(BY_DESIGN, "the foreground's edge drawn as the footage shows it, the key's clamp alone taken out", because="despill.edge false"))
    elif despill is not None and spill.get("max"):
        out.append(
            Measured(
                BY_DESIGN,
                f"edge despill (width {despill.width_px:g} px) corrected up to {float(spill['max']) * 100:.1f}% of the "
                f"foreground's edge (mean {float(spill.get('mean') or 0.0) * 100:.1f}%, {int(spill.get('frames') or 0)} frame(s))",
                because=_keyed_text(drawn_by.get("key")),
            )
        )
    if report.get("halo") is not None:
        edge = f" ({int(report['halo_edge'])} px long)" if report.get("halo_edge") else ""
        after = f", after despill width {despill.width_px:g} px" if despill is not None and despill.edge else ""
        crop = report.get("halo_crop") if isinstance(report.get("halo_crop"), dict) else {}
        shown = f"; its worst stretch at 100%: file `{crop['file_id']}`" if crop.get("file_id") else ""
        parts = report.get("halo_parts") if isinstance(report.get("halo_parts"), dict) else {}
        told = [
            text
            for text in (
                f"{float(parts['own']) * 100:.1f}% the subject's own colour, not counted" if parts.get("own") else "",
                f"{float(parts['clamped']) * 100:.1f}% counted where the clamp touched it" if parts.get("clamped") else "",
            )
            if text
        ]
        apart = f" ({'; '.join(told)})" if told else ""
        out.append(held("halo", f"halo {float(report['halo']) * 100:.1f}% of the foreground's edge{edge}{after}{apart}{shown}"))
    if report.get("rim") is not None:
        # The key's residue a dark edge leaves past what stands in front: grey where the insert is not.
        parts = report.get("rim_parts") if isinstance(report.get("rim_parts"), dict) else {}
        crop = report.get("rim_crop") if isinstance(report.get("rim_crop"), dict) else {}
        edge = f" ({int(parts['edge_px'])} px of its edge)" if parts.get("edge_px") else ""
        lit = f"; {float(parts['light']) * 100:.0f}% of it where the insert's light took its colour" if parts.get("light") else ""
        shown = f"; its worst stretch at 100%: file `{crop['file_id']}`" if crop.get("file_id") else ""
        out.append(
            held(
                "rim",
                f"grey rim {float(report['rim']) * 100:.1f}% of the band {screen_insert.RIM_FROM_PX:g}-"
                f"{screen_insert.RIM_PX:g} px past what stands in front{edge}, where the insert is not{lit}{shown}",
            )
        )
    if report.get("rim_surroundings") is not None or report.get("rim_whole") is not None:
        # The footage's own grey past what stands in front, off the keyed surface: never the key's residue.
        said = [
            f"{float(report[name]) * 100:.1f}% {words}"
            for name, words in (("rim_surroundings", "on the surroundings off the keyed surface"), ("rim_whole", "on the whole band"))
            if report.get(name) is not None
        ]
        out.append(Measured(FACT, f"grey past what stands in front, not gated: {'; '.join(said)}"))
    if report.get("rim_off") is not None:
        # The grey the compositing drew over the insert off the keyed surface, where the footage had colour: gated.
        out.append(
            held(
                "rim",
                f"grey drawn over the insert off the keyed surface {float(report['rim_off']) * 100:.1f}% of the band "
                f"{screen_insert.RIM_FROM_PX:g}-{screen_insert.RIM_PX:g} px past what stands in front, where the footage had colour",
            )
        )
    front = report.get("foreground") if isinstance(report.get("foreground"), dict) else None
    if front:
        skipped = int(front.get("skipped") or 0)
        crop = report.get("foreground_crop") if isinstance(report.get("foreground_crop"), dict) else {}
        # Its mask's confidence is a fact past ``screen_insert.LOW_FRONT_CONFIDENCE``; under it on enough frames, the
        # ``insert_ghost`` finding.
        out.append(
            held(
                "ghost",
                f"what stands in front covers up to {float(front.get('max') or 0.0) * 100:.0f}% of the quad on "
                f"{int(front.get('frames') or 0)} frame(s), kept in front of the insert and left out of the colours and "
                "the sharpness"
                + (
                    f"; colours not compared on {skipped} frame(s) where it covers over "
                    f"{screen_insert.COLOUR_MAX_FOREGROUND:.0%} of the quad"
                    if skipped
                    else ""
                )
                + _confidence_words(front.get("confidence"))
                + (f"; the insert drawn over it at 100%: file `{crop['file_id']}`" if crop.get("file_id") else ""),
            )
        )
    through = report.get("through") if isinstance(report.get("through"), dict) else None
    if through:
        # The source's own strokes drawn across what stands in front, read in the frame's pixels.
        strokes = report.get("through_crop") if isinstance(report.get("through_crop"), dict) else {}
        out.append(
            held(
                "through",
                f"its source's own strokes drawn across what stands in front on {float(through.get('max') or 0.0) * 100:.2f}% "
                f"of it at most ({int(through.get('frames') or 0)} frame(s); the gate: "
                f"{screen_insert.MAX_THROUGH_SHARE * 100:.1f}%)"
                + (f"; its worst stretch at 100%: file `{strokes['file_id']}`" if strokes.get("file_id") else ""),
            )
        )
    colour = report.get("colour") if isinstance(report.get("colour"), dict) else None
    if colour:
        light = colour.get("light")
        failed = int(colour.get("frames_failed") or 0)
        head = f"colour at light {float(light):g}" if light is not None else "colour"
        tail = f" ({failed} frame(s) past the colour gate)" if failed else ""
        out.append(
            held("colour", f"{head}: {colour_numbers(colour)}{tail}", because=f"light {float(light):g}" if light is not None else None)
        )
    # What its gates read of it (dev:video-render-wide-insert-gate): every frame or a sample, and the worst of them. A
    # worst frame past a gate a finding of the insert holds is that finding's, never a fact.
    if (covered := coverage_words(report)) is not None:
        read = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
        kinds = (_MEASURED_BY.get("colour" if gate == "colour_shift" else gate) for gate in read.get("worst_gates") or [])
        held_by = next((kind for kind in kinds if kind is not None and kind in found), None)
        out.append(Measured(FINDING, covered, kind=held_by) if held_by else Measured(FACT, covered))
    return out


def contradictions(checks: dict | None) -> list[str]:
    """What a render's *checks* say of an insert that its own report says the other way: an ``insert_flicker``
    - among its findings, or the harness's note (``render_checks.harness_of``) - on an insert whose report counts a
    flicker of 0 frames (``flicker_frames``, :func:`owned_flicker`). Empty for a report consistent with itself."""
    counted = {str(report.get("key")): int(report.get("flicker_frames") or 0) for report in insert_reports(checks)}
    out = []
    for said in [*findings_of(checks), *harness_of(checks)]:
        key = str((said.get("data") or {}).get("insert") or "")
        if said.get("kind") == INSERT_FLICKER and counted.get(key) == 0:
            out.append(f"insert {key}: insert_flicker at t={clock(said.get('at'))} beside its report's flicker of 0 frames")
    return out


def missing(
    reasons: dict[str, str],
    parts: dict[str, Any],
    document: dict,
    *,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
) -> list[Finding]:
    """An ``insert_missing`` finding for each screen insert the render could not draw (*reasons*: key -> why).

    *parts* are ``timeline.insert_sources``' (the overlay's own time in its
    scene); *document* the planned document, *offset* and *span* as in
    :func:`findings`. The insert is left out of the frames, so the audience
    would see what the footage drew there - a video model's own mark, never
    the template's: a failure, never a note.
    """
    from app.services.video_studio import timeline

    starts = {scene.get("id"): scene for scene in document.get("scenes") or []}
    out: list[Finding] = []
    for key, reason in reasons.items():
        scene_id = timeline.insert_scene(key)
        scene, part = starts.get(scene_id), parts.get(key)
        if scene is None:
            continue
        begin = float(scene.get("start") or 0.0)
        start = begin + float(getattr(part.overlay, "start", 0.0) or 0.0) if part is not None else begin
        length = getattr(part.overlay, "duration", None) if part is not None else None
        end = start + float(length) if length else begin + float(scene.get("duration") or 0.0)
        if span is not None and not (end > span[0] and start < span[0] + span[1]):
            continue
        mark = getattr(part, "ref", None) if getattr(part, "kind", None) == "asset" else None
        out.append(
            Finding(
                INSERT_MISSING,
                max(0.0, start - offset),
                max(0.0, end - offset),
                scene_id,
                f"insert missing at t={clock(max(0.0, start - offset))}: Scene {scene_id}: {timeline.insert_label(key)} "
                f"is not drawn ({reason}) - the footage shows what its model drew there instead"
                + (f" of the template's {mark}" if mark else ""),
                FAIL,
                {"insert": key, "reason": reason[:500], **({"asset": mark} if mark else {})},
            )
        )
    return out


def surface_lost(error: Any, document: dict, *, offset: float = 0.0) -> Finding:
    """The finding of a tracked surface lost (``insert_tracking.InsertTrackLostError``): at its scene, in the MP4's time."""
    scene_id = getattr(error, "scene_id", None)
    scene = next((s for s in document.get("scenes") or [] if s.get("id") == scene_id), None)
    when = float(scene.get("start") or 0.0) + float(getattr(error, "scene_seconds", 0.0) or 0.0) if scene else 0.0
    return Finding(
        SURFACE_LOST,
        max(0.0, when - offset),
        None,
        scene_id,
        str(error),
        FAIL,
        {"footage_frame": getattr(error, "frame", None), "footage_seconds": round(float(getattr(error, "seconds", 0.0)), 3)},
    )
