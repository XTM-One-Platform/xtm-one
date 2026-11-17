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

"""A picture or a video mapped onto a surface of the footage, frame by frame.

A video model draws a monitor, a billboard or a poster well; it cannot draw
the real product legibly on it. A ``screen_insert`` overlay puts the real
thing there after the shot is made - a recording, another AI shot, a video
file, a picture or a brand mark of the template - on the surface given as its
``quad``: its four corners in the scene's footage, normalised (0-1 across and
down), top left, top right, bottom right, bottom left. This module draws one
frame of it, with Pillow and numpy, never in a browser: the render's frame
server and the editor's preview build (``insert_builds``) both draw through
:func:`compose`, so the two never disagree.

* **The bezel lock (``surface_refine``).** With footage under it, the
  quad - the spec's, or the tracker's for the frame - is a seed: the
  surface's inner bezel edges are found in a band around it and the quad
  snapped to them; the given quad's error against the bezel is the frame's
  ``alignment``. A static insert is locked once, on its first footage frame
  (:class:`Lock`, a keyed one's :class:`Fill`), and drawn into that quad on
  every frame; only a tracked insert (``track: true``) finds its bezel again
  frame by frame, the correction carried to the next frame.
* **Homography.** The source - cut to the quad's proportions (``cover``) or
  kept whole on transparency (``contain``, ``pictures.fitted``: the surface
  shows around it, never a black card; a keyed layer fills those bands)
  - is mapped onto the quad by the eight
  perspective coefficients of ``pictures.perspective_coefficients``: the
  inverse map Pillow samples. A picture keeps its alpha.
* **Sharp at the output size.** The source is brought to the size it
  is drawn at (LANCZOS), read at twice the box's size through the transform
  (Pillow reads bilinear whatever it is asked) and brought to the box once
  with LANCZOS: a one-pixel stroke keeps its edge at the render's resolution.
* **Premultiplied alpha edges.** The source is sampled premultiplied (Pillow's
  ``RGBa``) inside a transparent border, so the quad's edges come out
  antialiased with no dark fringe.
* **The matte.** What stands in front of the surface stays in
  front; the rest of the surface is painted flat under the insert. With a
  ``key`` - its colour and the surface's spread read off the first footage
  frame (:func:`keyed_on`) - the matte (``screens.key_matte``) is the lower
  median over the :data:`MATTE_WINDOW` footage frames around the one shown
  (:class:`Matting`, each footage frame's raw matte read once whatever frames
  show it), the foreground the non-key regions that reach the
  surface's rim or hold :data:`FOREGROUND_MIN_SHARE` of it, its edge
  feathered; what is not the key in a pixel is unmixed by the colour
  difference (``screens.key_share``), never past black, and despilled by the
  clamp (``screens.despill_clamp``): the layer then carries the whole surface
  and :data:`KEY_MARGIN` around it, the footage there despilled. In that
  margin (:class:`Band`) the key joined to the surface inside the quad
  is the surface running past a quad too small, left for the residual-key
  gate; the key the bezel parts from the surface is the footage's own key
  colour past the bezel, which no quad can cover: keyed at the key's
  tolerance, feathered by its feather and filled with the bezel's colour on
  that side. Within :data:`RESIDUAL_MARGIN` of the quad, the key's hue the
  layer still shows takes the bezel's colour on its side
  (:func:`fill_margin`), but for the surface running past a quad too small
  (:func:`running_past`); past it, it is despilled toward the luminance
  around it on every frame (:func:`_despill_margin`), and what the
  margin lost of the key is audited (``despilled``). Without one,
  the surface's clean look over the insert's frames is the reference
  (:class:`Surface`, :meth:`Surface.of_plate`: the median of the frame
  it comes in on and its plate): a frame is read on the same plane (its
  quad, and a rim past it), and what differs from the reference and enters
  the surface through its rim - a cat, a hand - is in front, with the weaker
  change joined to it (dark fur over a dark screen); a change inside the
  surface alone (what the model drew on a screen) is the surface's own and
  is covered.
* **What stands in front is never drawn over.** Where the mask every
  gate reads marks something in front, less its edge ring
  (:func:`front_core`), the insert is drawn at nothing (:func:`in_front`),
  keyed or not - the frame's own reading kept in front whatever its window
  says; the ring keeps the matte's soft edge, a key's share of each pixel.
  How sure that mask is, is audited (``front_confidence``).
* **What stands in front at the frame's own resolution.** An
  occlusion's grid mask, brought to the box, is read again pixel by pixel
  within :data:`FULL_BAND_GRID` grid pixels of its edge - grown into the
  weak change joined to it: a soft edge's fringe, a tuft the grid opened away
  - and closed with a kernel :data:`FRONT_FULL_CLOSE_PX` wide at 1080p
  (:func:`close_width`), its holes filled (:meth:`Surface.front`); its edge
  is the subject's faintest fringe, so none of it shows the surface and the
  feather falls past it. What the drawing still puts of the source's own
  strokes inside it is audited (``through``, :func:`through_share`), and on
  a keyed insert the key's hue past the quad on the whole footage frame
  (``key_spill``, :func:`key_spill`).
* **A key that does not blink.** A keyed insert that is not tracked
  keys nothing frame by frame: its bezel is locked and its surface read once
  (:func:`static_fill`, a :class:`Fill`, its margin read on the same frame),
  the source drawn over that filled surface on every frame, and what enters
  the quad through its rim stays in front through the occlusion above, which
  reads no key, its edge feathered by the key's feather. A tracked one keys
  frame by frame through the median; how much of the matte as drawn holds
  from the frame before - what toggled back taken out, ``screens.edge_hold``
  -   is audited (``FrameAudit.edge_hold``): under ``screens.MIN_EDGE_HOLD`` the
  key flickers.
* **A keyed surface never shown raw.** A keyed insert is held on its
  surface over its whole scene (``timeline.hold_surfaces``), both transitions
  included: the page draws its layer whole on every frame and names how much
  of the insert shows (:func:`compose`'s *visibility*), the rest of the
  surface in the colour it shows with nothing on it (:func:`screen_off`: the
  bezel's dark colour, else black, the footage's light over it) - before the
  insert comes in, after it goes and through its fades. A transition then
  blends the scene's finished frame: the insert is never faded on its own
  over the key. A frame partly shown is audited for its surface alone
  (``FrameAudit.visibility``), and the render reads the key left on the
  encoded frames of every transition too (:func:`key_left`).
* **The contain bands and the foreground's edge.** The clamp
  despill turns a key the model painted off the primary (a green leaning
  to cyan) into a colour still of the key's hue to the gates, which no
  tolerance or feather changes. So a keyed layer never shows the surface
  in the bands a contain fit leaves inside the quad (:func:`contained_quad`):
  they are filled under the source (:func:`band_backdrop`) with the
  source's own edge colour along them when that edge is opaque and flat,
  else the bezel's dark colour (:func:`bezel_colour`), else black - the
  footage's light laid over the fill as over the source; a source whose
  edge is transparent (a mark on transparency) keeps the keyed surface
  around it, its board. On the edge of what stands in front, a footage
  pixel the clamp leaves on the key's hue or its complement takes its clean
  neighbours' colour (:func:`edge_despill`) - unless that colour is the
  subject's own (:func:`edge_of`), which the halo gate never counts:
  a hue holding :data:`OWN_HUE_MIN_SHARE` of the subject's pixels over its
  clip (:class:`Subject`, read once: at save, :func:`subject_of`), a hue
  the author declares (``despill.own_hues``), the key's hue running on at
  its saturation into the subject (spill falls off), and the complement
  wherever the compositor's clamp did not make it.
* **The edge of what stands in front.** A subject in front of the
  surface carries its colour on its own edge - fur, hair, a sleeve - which
  the matte keeps in front, and the clamp's channel rules leave a key
  painted between a primary and a secondary (a green leaning cyan) of its
  hue there. Before the despill above, the key's colour is limited out of
  that edge (:func:`edge_limit`: its chroma along the key's own taken out,
  its luminance kept) the insert's ``despill.width_px`` and the key's
  feather past it, fading to nothing as far into the subject; the edge then
  takes a little of the insert's light behind it (:func:`light_wrap`, 2 px
  of a 1080p frame). The insert's ``despill.edge: false`` draws that edge as
  the footage shows it. The share corrected is audited per frame
  (``edge_spill``), and the save reads the same edge on the insert's first
  frame (:func:`edge_spill`, ``surface_refine.measure_bezel``).
* **The key's residue on that edge.** The key's share a pixel keeps
  there - an unmix capped by a matte the window holds in front, no unmix
  at all where a static insert's occlusion holds it in front, a few pixels
  past a dark subject - the clamp turns into a grey the footage never had,
  4-12 px of it at 1080p, no hue outlier. Before the light wrap, within
  :data:`RIM_PX` of the part of what stands in front the key reads clear,
  a pixel the frame's own key matte reads part key whose colour has no
  chroma left takes the insert's own pixel, blended by that matte
  (:func:`key_residue`, in both keyed paths, whatever ``despill.edge``):
  the band reads as the insert behind a soft edge. The grey rim gate reads
  that band on the keyed surface alone: the footage's own grey past
  what stands in front, off the surface (a desk, a console the quad
  overlaps) or already grey before any despill, is a fact, never the key's.
* **Glow.** A bloom of the inserted picture's light: what is drawn of it - its
  alpha silhouette, matted - reduced to an eighth, blurred, tinted with the
  insert's mean colour at a low alpha (:data:`GLOW_ALPHA`) and clipped
  to the bezel (the quad grown by :data:`GLOW_REACH` of its width), so it
  never halos the wall; the page draws it under the sharp frame with a
  ``screen`` blend at the overlay's ``glow``. A logo glows as a halo around
  its mark and its wordmark, never as the quad's rectangle. A keyed layer
  covers its surface whole, so there the halo's light is laid on the
  despilled surface in the layer itself (:func:`_halo`, the page's blend),
  never hidden under it.
* **A camera push.** An insert's ``zoom`` draws its source larger
  within its quad: the page names the insert's own second (``&o=``), and the
  source is cut to the window the push frames then (:func:`zoomed`:
  :meth:`Zoom.scale_at` eased from 1 to its scale, centred on its focus and
  kept inside the source) before it is fitted - the quad, the matte and every
  gate as without one.
* **The footage's light.** The overlay's ``light`` (0-1, default
  ``spec.INSERT_DEFAULT_LIGHT``) lays the light and the reflections the
  footage shows on the surface over the insert: its luma, blurred to its low
  frequencies over the surface alone and taken above the surface's own level
  (:func:`ambient_light`), screen-blended over the source at that strength
  (:func:`lay_light`) - light added, the same on every channel, so a surface
  lit evenly adds nothing and the insert's blacks take no cast of the
  surface's colour (the key's green, the room's blue).

Every frame drawn is audited (:class:`FrameAudit`): the share of the quad the
insert is drawn on where its matte says the foreground is (``insert over
foreground``: seen through included, on any frame past 0.5 %), how
much of a brand mark's own pixels still show (``logo
lost``), what the compositing did to the insert's colours
(:func:`colour_fidelity`: the quad as composited held to the source drawn into
the same pixels - its luminance, its black level, its hue or its cast, its
saturation and its contrast; ``insert colour shift`` when the blacks are
lifted or the hue turned past a threshold), once per picture flat bands on two opposite sides of
what it draws (``insert padded``, :func:`padded_sides`), and the gate of
The given quad's error against the bezel, the matte's coverage and
the insert's luma (flicker), the key's hue left in or around the surface
(where it sits - inside the quad, the contain bands, the
surface past it, the margin past the bezel - and on which side, the side
holding most of it kept at 100 %), the sharpness of the insert region
against its source, the hue outliers along the foreground's edge (halo; the
footage's own hue left out and counted apart), the grey band past it
where the insert is not (:func:`_rim_share`: ``rim``), and the share
of the source's content a ``cover`` fit cuts away (``insert cropped``,
``pictures.cover_cuts``) - and the bands a contain fit leaves and
what filled them.
Every gate reads the same masks of the frame, computed once
(:class:`GateMasks`): the source's own pixels of the key's hue (a product UI's
green ring is what the insert shows, never the key left), the contain bands
filled under it, what stands in front grown past its edge ring, its share of
the quad, the margin. The residual key leaves the first three out, the halo
the edge over the source's own hue; the colours and the sharpness are read
clear of the foreground and the bands, and not compared at all on a frame
whose foreground covers more than :data:`COLOUR_MAX_FOREGROUND` of the quad.
The render turns them into findings and into each insert's report
(``insert_checks``), as it does the source's pixels per pixel of its quad at
the render's size (``insert soft``, :func:`density`).

A key named by its colour keys on the shade of it the surface shows on the
insert's first footage frame (:func:`keyed_on`, ``screens.sample_key``),
logged; a ``#rrggbb`` is keyed as written.

The compositor page asks for a frame at a size - its host's on-screen size in
device pixels (``insert/<key>/<frame>.png?w=&h=``); the answer covers the
quad's bounding box at that size (:func:`bbox`), which the page places with
the same arithmetic, so frame and page always agree.

A tracked insert (``track: true``, ``insert_tracking``) has a quad per
frame of its scene's footage: the page names the footage frame it shows
(``&f=``), the frame is drawn into that frame's quad, and every frame covers
one box - the one all its quads lie in - so the page places them all alike.
A matted insert names it too: its matte reads that footage frame.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field, fields
from typing import Any

from app.core.color import format_hex
from app.services.video_studio.pictures import fitted, perspective_coefficients, quad_size
from app.services.video_studio.spec import HUE_NAMES, INSERT_DEFAULT_LIGHT, INSERT_DESPILL_WIDTH

logger = logging.getLogger(__name__)

#: Moved when an insert's frames are drawn differently (2: a keyed insert's contain bands filled, the foreground's edge
#: despilled; 3: the key's hue in the residual margin filled with the bezel's colour; 4: the key's colour
#: limited out of the foreground's edge and the insert's light wrapped onto it; 5: the subject's own colours left
#: on that edge; 6: a static unkeyed insert's bezel locked once, on its first frame; 7: a keyed insert held on its
#: surface over its scene, composed over the bezel's colour where it does not show; 8: what stands in front is
#: never drawn over, an unkeyed one's mask grown through the weaker change joined to it and kept frame by frame;
#: 9: a static insert's bezel read on every frame against its lock, its drift audited; 10: the key's
#: residue on the edge of what stands in front drawn as the insert; 11: a seam of the key's hue straight across
#: the quad counted as residual key whatever mask it falls in; 12: a subject standing still over the plate taken
#: out of the surface's reference and read as in front; 13: that reference filled from the plate frames showing the
#: surface clear of it, the occluder closed into one body, an occlusion's edge ring the feather deep; 14: the
#: occlusion's mask read again at the frame's own resolution around the grid's and closed there; 15: a
#: recording scene's automatic camera still and wide before its transition out, never moving under a crossfade;
#: 16: the grey rim read on the keyed surface alone, the footage's own grey surroundings a fact; 17: an
#: unkeyed insert's mask read past its subject, its halo and its lag audited; 18: a recording scene played slowed
#: shows the painted frame nearest each output frame, never two cross-dissolved; 19: a pocket of the key a
#: keyed insert's occlusion closed into what stands in front keyed out and the insert drawn in it): a
#: render's segments drawn before are never joined to frames drawn after (``compositor``'s digest); 20: a frame's audit
#: reads a line of the key's hue along each edge of a keyed quad, inside and past it (``FrameAudit.residual_edge``);
#: 21: a layer never draws over the quad of another insert of its scene (``FrameAudit.others_cut``); 22: a recording
#: scene's motion-blur sub-frames all show the take frame of their output frame's centre, never two averaged into one;
#: 23: a video insert's frame and a tracked or matted insert's footage frame are picked at that centre too; 24: the
#: grey the compositing drew past what stands in front off the keyed surface, where the footage had colour, gated
#: (``FrameAudit.rim_off``); 25: an unkeyed insert's contain bands read for a chroma key colour the footage shows
#: there (``FrameAudit.band_key``).
DRAWING_VERSION = 25
#: The largest host size a frame is drawn at, either side, in pixels (a 4K frame zoomed in stays under it).
MAX_SIDE = 4096
#: The transparent border the source is sampled in: the quad's edges ramp to nothing over it.
EDGE_PAD = 2
#: A source within this share of the size it is drawn at is read as it is; another is brought to that size first.
RESIZE_TOLERANCE = 0.05
#: The warp is drawn at this many times the box's size and brought to it once (LANCZOS) while the box holds at most
#: :data:`SUPERSAMPLE_BUDGET` pixels: a one-pixel stroke of the source keeps its edge at the output size
#: (Pillow's perspective transform reads bilinear whatever filter it is asked; the supersample is what sharpens).
#: The budget bounds the frame drawn at twice the size: a 4K box of 2000 x 2000 fits, a 4K frame whole does not.
SUPERSAMPLE = 2
SUPERSAMPLE_BUDGET = 4_000_000
#: The box a matted insert's frames cover: its quad grown by this share of the frame's width, room for the
#: bezel lock (``surface_refine.BAND``, its corner tolerance) and the key margin; the page places the same box.
BOX_MARGIN = 0.05
#: A quad snapped to its bezel is drawn this many pixels past the edge found (mid-ramp): the insert's own antialiased
#: edge then covers the outer half of the surface's blurred edge, and no rim of the key stays along the bezel.
EDGE_GROW_PX = 1.0
#: The margin past the surface the despill reaches and the residual key is looked for in, a share of the frame's width.
KEY_MARGIN = 0.02
#: In that margin, a pixel keyed past this is the key itself, never the key's bleed on the bezel - the matte saturates
#: before a pixel is all key, so a blurred edge reads under it.
MARGIN_BLEED_MATTE = 0.98
#: A margin pixel keyed at least this much is of the key: joined to the surface inside the quad it is the surface
#: running past a quad too small, left as it is for the residual-key gate; parted from it by the bezel it is the
#: footage's own key colour past the bezel, which the insert cannot cover - keyed and filled with the bezel's colour.
BAND_KEY_MATTE = 0.5
#: The bezel's colour on a side is read on the margin pixels that are not the key within this share of the margin
#: from the quad: the bezel next to it, rather than the wall past the footage's key colour.
BEZEL_SAMPLE_SHARE = 0.5
#: The sides of a quad, its edges in order from its top left corner; and where a residual key is said to sit: inside
#: the quad where the source is drawn, in the bands a contain fit leaves inside it, the surface running past
#: it, or the footage's key colour past the bezel the key did not take.
SIDES = ("top", "right", "bottom", "left")
RESIDUAL_REGIONS = ("inside", "contain", "edge", "band")
# ── The contain bands and the foreground's edge ──
#: A keyed insert's quad is filled under its source where a contain fit leaves bands, so the surface never shows
#: there: with the source's own colour along them when that edge is opaque and one flat colour, each channel of each
#: edge spreading less than this many grey levels and the two edges as close ...
EDGE_FLAT_SPREAD = 6.0
#: ... else with the bezel's colour when its luma is under this (the screen's own black as the footage shows it), else
#: black - never a colour of the key's hue, which the residual-key gate would read as the key.
MAX_BACKDROP_LUMA = 80.0
#: What fills a contain fit's bands (``FrameAudit.band_fill``): the source's edge colour, the bezel's, black; the keyed
#: surface around a source whose edge is transparent (a mark on its board, never a card); the footage, unkeyed.
BAND_FILLS = ("source edge", "bezel", "black", "surface", "footage")
#: Bands left to the footage show a chroma key colour where a pixel's key channel leads the two others by more than
#: this many grey levels (green: G - max(R, B); blue: B - max(R, G); magenta: min(R, B) - G) ...
BAND_KEY_LEAD = 40.0
#: ... on at least this share of the bands' pixels: a green screen left unkeyed, two saturated bars on it.
BAND_KEY_MIN_SHARE = 0.5
#: The edge despill: within this many pixels of a 1080p frame (twice the key's feather added) of the foreground's
#: boundary, a footage pixel still of the key's hue - or its complement - after the clamp takes the colour of its clean
#: neighbours in front, unless that colour is the subject's own (:class:`Edge`): left as it is, and never
#: counted by the halo gate.
EDGE_DESPILL_PX = 4.0
# ── What stands in front's own colours ──
#: A hue holding at least this share of the subject's pixels over its clip (:class:`Subject`) is its own on every frame,
#: never spill - a pink muzzle, a green collar, however little of it a frame shows ...
OWN_HUE_MIN_SHARE = 0.02
#: ... the wheel read in bins this many degrees wide, a hue holding its bin and the two beside it: a colour split
#: between two bins is one colour.
OWN_HUE_BIN_DEGREES = 10.0
#: An own hue an author declares as ``#rrggbb`` (``despill.own_hues``) covers its hue this many degrees either side; a
#: name covers its span of the wheel (``spec.HUE_NAMES``).
OWN_HUE_DECLARED_DEGREES = 15.0
#: The subject is read on at most this many frames of its clip, spread over it, at most this many pixels wide: its
#: colours, never its detail ...
SUBJECT_FRAMES = 48
SUBJECT_WIDTH = 640
#: ... and fewer of its pixels than this say nothing of them.
SUBJECT_MIN_PIXELS = 400
#: Spill is an edge gradient, never a colour: a pixel of the key's hue or its complement on that edge is the
#: subject's own when at least this share of the subject's interior around it holds that hue ...
RUN_ON_SHARE = 0.25
#: ... no less saturated than it by this much: the surface's light is most saturated on the edge and falls off into the
#: subject, a colour of its own runs on.
SPILL_FALLOFF = 0.15
#: The compositor's clamp touched a pixel it moved by at least this many grey levels on a channel: only there is
#: the key's complement spill - an inverting despill's magenta is the clamp's, never the footage's.
CLAMP_MOVED = 6.0
# ── The edge of what stands in front, its spill limited and the insert's light wrapped onto it ──
#: The edge limit reaches this many pixels of a 1080p frame past the foreground's boundary against the surface (the
#: key's feather added) when the insert names none (``spec.InsertDespill.width_px``), and fades to nothing as far into
#: what stands in front: its interior is never touched.
EDGE_SPILL_PX = INSERT_DESPILL_WIDTH
#: A pixel there whose colour the limit moved by at least this many grey levels was corrected (``FrameAudit.edge_spill``).
EDGE_SPILL_MOVED = 1.0
#: Spill is read by colour difference too, never by hue alone: a pixel there whose key channel leads the others
#: (``screens.key_share``, its excess over the key's own) by more than this is spill whatever its hue - a skin tone
#: mixed with the key reads yellow-green, 40-60 degrees off the key's hue, where the hue's weight alone corrected
#: half of it (#10573) ...
EDGE_SPILL_LEAD = 0.02
#: ... corrected in full from this lead on.
EDGE_SPILL_LEAD_FULL = 0.08
#: The light wrap: a pixel of what stands in front on its boundary against the surface takes this share of the insert
#: behind it ...
LIGHT_WRAP_SHARE = 0.25
#: ... fading to nothing this many pixels of a 1080p frame into it: a real screen's light on a real edge, never a glow.
LIGHT_WRAP_PX = 2.0
#: A halo finding keeps its worst edge as a 100 % crop: the outliers' largest cluster and this many pixels of a 1080p
#: frame around it, at least :data:`HALO_CROP_MIN_PX` a side.
HALO_CROP_MARGIN_PX = 24.0
HALO_CROP_MIN_PX = 64
#: The glow is drawn at this fraction of the host's size: a bloom has no fine detail.
GLOW_SCALE = 8
#: The glow's blur radius, as a share of the host's short side.
GLOW_RADIUS = 0.035
#: The glow reaches this share of the quad's width past its edges - the bezel - and no farther.
GLOW_REACH = 0.02
#: The glow's alpha at its brightest, before the overlay's ``glow``: a tint of the insert's mean colour, never a copy.
GLOW_ALPHA = 0.5
#: A source drawn at fewer of its own pixels per pixel of its quad than this, at the render's size, is soft.
MIN_DENSITY = 0.8
#: A ``cover`` fit cutting more than this share of the source's content (``pictures.content_box``) crops the insert.
MAX_CONTENT_CUT = 0.01
#: A contain fit's padding: transparent, so the surface shows around the picture - never a black card. A keyed
#: layer fills it under the source (:func:`band_backdrop`).
LETTERBOX = (0, 0, 0, 0)

#: The feather of the occlusion matte, in pixels of a 1080p frame (a key gives its own): 1.0 - a subject's
#: edge over an insert is a pixel wide at 1080p, anything softer reads as a halo or a see-through edge.
OCCLUSION_FEATHER = 1.0
#: The surface is compared on a grid at most this long (its longer side), its quad grown by :data:`RIM`: 512
#: so a 1080p quad's matte edge is read at about 2 px, never a 3-4 px blur the insert shows through as a halo.
SURFACE_SIDE = 512
#: A patchy reading of what stands in front is closed over this many grid pixels: one body, no sieve.
FRONT_CLOSE_PX = 3
# ── What stands in front, read at the frame's own resolution (:meth:`Surface.front`) ──
#: The grid's mask, brought to the frame, is read again pixel by pixel within this many grid pixels of its edge - where
#: the grid's own pixels and its openings (a tuft thinner than two of them is opened away) leave the edge unsure - and
#: grown there into the pixels joined to it that differ from the clean surface by the weaker change
#: (:data:`WEAK_CHANGE_MIN`, :data:`WEAK_CHANGE_SPREAD`): a soft edge's fringe, a tuft finer than a grid pixel ...
FULL_BAND_GRID = 2.5
#: ... the frame read through a Gaussian of this many pixels of a 1080p frame (the encoder's noise), the surface at its
#: closest within a pixel (a track a pixel off moves nothing) ...
FULL_SIGMA_PX = 1.0
#: ... and the mask closed with a square kernel this many pixels wide at 1080p (scaled with the frame, odd, never under
#: 3) and its holes filled before the insert is drawn: a gap of a pixel or two lets no stroke of the source through.
FRONT_FULL_CLOSE_PX = 3.0
#: How far past the quad's edges the surface's rim is read, as a share of its size: what enters the surface crosses it.
RIM = 0.2
#: A grid pixel differs from the reference when, at the closest of its neighbours, it is this many grey levels off ...
CHANGE_MIN = 26.0
#: ... and this many times the frame's median difference.
CHANGE_SPREAD = 4.0
#: A change entering through the rim smaller than this share of the grid is noise (a compression block).
MIN_OCCLUDER_SHARE = 0.002
#: The surface's clean reference is the median of this many footage frames spread over the frames the insert is drawn
#: on (:meth:`Surface.of_plate`): a subject standing in front on the frame it comes in on, gone from most of
#: them, is no part of the surface.
PLATE_FRAMES = 7
#: A subject standing still in front of the surface over the whole plate - a cat sat on the desk under the screen
#: - is in the plate's median, so it would read as surface and be drawn over (observed on the cats film, scenes
#: demo-opencti-graph, demo-openaev-scores and demo-openaev-remediation: the cat's head under the UI, the gate green).
#: Inside the quad, a connected region this far off the surface's own flat colour (grey levels, and this many times
#: the median difference), touching the quad's edge and holding at least :data:`MIN_OCCLUDER_SHARE` of the quad, is
#: such a subject (:func:`still_subject`): filled from the surface around it in the reference, and an entry zone for
#: the occluder like the rim. One holding more than :data:`STILL_MAX_SHARE` of the quad is what the surface itself
#: shows (a UI the model drew), not a subject.
STILL_CHANGE_MIN = 26.0
STILL_CHANGE_SPREAD = 4.0
STILL_MAX_SHARE = 0.4
#: A weaker change joined to one that came in through the rim is the same thing in front: dark fur over a dark
#: screen differs from it by less than :data:`CHANGE_MIN`, never from the bright wall it crossed to get there. Weak:
#: this many grey levels off the reference ...
WEAK_CHANGE_MIN = 8.0
#: ... and this many times the frame's median difference ...
WEAK_CHANGE_SPREAD = 2.5
#: ... grown into only where it stays once opened by this many grid pixels: the thin ring of weaker change round a
#: strong edge is that edge's own blur, never more of the subject.
WEAK_MIN_THICKNESS = 2
#: The surface moved more than this (grid pixels) off its reference: a camera an untracked quad does not follow, not
#: something in front of it - nothing is taken for an occluder in that frame. Searched within ``DRIFT_SEARCH``.
MAX_DRIFT = 2.0
DRIFT_SEARCH = 16
#: A shift explains the frame better than none only when it leaves this much less of the rim changed (a share of the
#: rim): a camera move empties most of the rim at the right shift, while an occluder's edge pixels crossing the
#: threshold one way or the other decide nothing.
DRIFT_MIN_GAIN = 0.02

#: A frame fails when the insert is drawn on more than this share of its quad where the matte says the foreground is
#: (0.5 %, its edge ring left out) ...
MAX_COVERED_SHARE = 0.005
#: ... drawn at more than this share of it: a subject seen through the insert is drawn over too.
DRAWN_MIN_ALPHA = 0.05
#: The quad's edge band :func:`edge_line` reads, this many pixels of a 1080p frame either side of its antialiased
#: pixels (#11460) ...
EDGE_LINE_PX = 3.0
#: ... and a frame fails when the insert is drawn there over what stands in front on more than this many pixels per
#: pixel of the quad's outline (``FrameAudit.edge_line``): a 1 px line along 1 % of the outline; a stray pixel never.
MAX_EDGE_LINE_SHARE = 0.01
#: A keyed foreground pixel whose matte reads under this is clear of the key: told from the surface by the key alone.
KEY_CLEAR_MATTE = 0.1
#: A pixel inside what stands in front whose frame's own key matte reads at least this is the key: a pocket of
#: the surface the mask closed over - between a subject and the bezel's corner -, keyed out whatever it joins.
POCKET_KEY_MATTE = 0.9
#: Under this confidence (``FrameAudit.front_confidence``), what stands in front is told from the surface mostly by its
#: link to what crossed its rim, not by its own difference - dark on dark: said in the insert's report ...
LOW_FRONT_CONFIDENCE = 0.5
#: ... and, on a frame where it covers more than this share of the quad, a ghost (``FrameAudit.ghosted``): a
#: subject the mask guesses at, which shows the insert through it wherever the guess falls short.
GHOST_MIN_FOREGROUND = 0.02
# ── The source seen through what stands in front (:func:`through_share`) ──
#: Inside the core of what stands in front, the high frequencies the drawing added to the frame (the composited frame
#: less the footage, the luma less its Gaussian of this many pixels of a 1080p frame) ...
THROUGH_SIGMA_PX = 1.0
#: ... are held to the source's own at that place in tiles this many pixels of a 1080p frame a side, a tile read where at
#: least this share of it lies in the core ...
THROUGH_TILE_PX = 8
THROUGH_TILE_SHARE = 0.5
#: ... the source holding an edge there (its high frequencies' spread, grey levels) and the drawing having added some a
#: viewer sees (their RMS, grey levels) ...
THROUGH_MIN_SOURCE = 4.0
THROUGH_MIN_ADDED = 2.0
#: ... correlated with the source's past this (Pearson): the source's own strokes drawn across what stands in front ...
THROUGH_MIN_CORRELATION = 0.3
#: ... and a frame fails past this share of that core (``insert_through_foreground``).
MAX_THROUGH_SHARE = 0.001
#: A keyed frame failing it on the edge of what stands in front alone - every hit within this many pixels of a 1080p
#: frame of its boundary, the tile's reach added - is drawn again with the matte refined there (:func:`refined_front`,
#: #10573): a guided filter of this radius on the footage's luma ...
REFINED_MATTE_PX = 3.0
#: ... smoothing a flat window by this much (luma 0-1, squared) ...
REFINED_MATTE_EPS = 1e-3
#: ... its pixels past this opacity in front, the insert never drawn on them.
REFINED_OPAQUE = 0.9
# ── The footage past the subject inside its matte (:func:`matte_halo`, :func:`matte_lag`) ──
#: Inside an unkeyed insert's mask of what stands in front, a band from its edge inward this many pixels of a 1080p
#: frame deep (at least :data:`MATTE_HALO_MIN_PX`) is read: where the drawn frame there is the footage and the footage
#: is the clean surface - no subject - the insert should have been drawn and was not, a dark halo or a ghost of the
#: subject beside it.
MATTE_HALO_BAND_PX = 12
MATTE_HALO_MIN_PX = 2
#: How close (max over the channels, grey levels) the drawn frame is to the footage, and the footage to the clean
#: surface, for a pixel of that band to be the surface left showing.
MATTE_HALO_TOLERANCE = 12.0
#: A frame fails past this share of the mask's edge length with the surface left showing beside it ...
MAX_MATTE_HALO_SHARE = 0.03
#: ... or past this share of the frame in one connected region of it (``insert_matte_halo``).
MAX_MATTE_HALO_REGION = 0.0005
#: The mask's centroid against the subject's read again from the footage at full resolution, in pixels of a 1080p
#: frame: past this, the mask lags or leads the subject on that frame (``insert_matte_lag``).
MAX_MATTE_LAG_PX = 4.0
#: The lag is read only where the footage tells the subject from the surface over this share of its mask (a subject
#: dark on a dark screen is not) ...
MATTE_LAG_TOLD_SHARE = 0.5
#: ... and only when the subject left past the mask, the insert drawn over it, is this share of the subject: a mask that
#: covers its subject whole lags nothing.
MATTE_LAG_UNCOVERED_SHARE = 0.05
#: What stands in front of a legible insert (``spec.INSERT_LEGIBILITY``) past this share of its quad on a frame hides
#: part of what it is there to show (``insert_occluded``) ...
OCCLUDED_SHARE = 0.25
#: ... and past this one on any frame, whatever the share of frames.
OCCLUDED_PEAK = 0.40
# ── The key past the quad (:func:`key_spill`) ──
#: A keyed insert's key is read over its whole footage frame: a connected region of the key's hue (:func:`_key_hued`)
#: past the quad and the layer's margin around it (:data:`KEY_MARGIN`), clear of every other insert's surface on the
#: footage and of the subject's own colours, larger than this share of the frame, is the key spilled past the screen -
#: a glossy floor reflecting it, a wall it lights (``key_spill_outside_quad``) ...
SPILL_MIN_SHARE = 0.002
#: ... read on the frame brought to about this many rows: a region that large holds no finer detail.
SPILL_ROWS = 270
#: Flat bands (each line one colour, within :data:`FLAT_SPREAD` grey levels) on two opposite sides, each at least
#: this share of the picture, are a letterbox: the picture is padded.
PAD_MIN_SHARE = 0.03
FLAT_SPREAD = 4.0
#: A band within this many levels (on every channel) of the picture's border on its other two sides is that border's
#: surface going on - a plate the mark sits on, never a card baked into the picture.
BAND_SAME_LEVELS = 12.0
#: A pixel of a band is opaque: a card baked into a picture is, a glow or a shadow fading into the surface is not.
OPAQUE_ALPHA = 0.95
#: A brand mark shows at least this share of its own opaque pixels in a frame, or it is lost there.
LOGO_MIN_VISIBLE = 0.9

# ── The light and the colours ──
#: The footage's light is read at this share of the quad's short side (a Gaussian's sigma): its reflections and its
#: glare, never its detail - what the model drew on the screen, an edge, the encoder's noise ...
LIGHT_SIGMA = 0.08
#: ... on a grid at most this long (its longer side): it holds nothing finer ...
LIGHT_GRID = 96
#: ... above the surface's own level, this percentile of that light over the surface: a surface lit evenly lays no
#: light over the insert, whatever its colour and its brightness.
LIGHT_FLOOR_PERCENTILE = 5.0
#: The insert as composited is held to its source drawn into the same pixels (:func:`colour_fidelity`): the source's
#: darkest share of pixels is its black level ...
BLACK_SHARE = 0.1
#: ... lifted past this share of the full scale, the insert's blacks are washed out ...
MAX_BLACK_LIFT = 0.05
#: ... its mean hue turned past this many degrees (a source with a hue of its own, ``screens.MIN_HUE_CHROMA``) ...
MAX_HUE_ROTATION_DEGREES = 5.0
#: ... or, on a source with none (a dark UI, a grey), a cast past this many grey levels of chroma: it is tinted.
MAX_CAST = 6.0
#: A source whose luma spreads over less than this (its 5th to its 95th percentile, grey levels) has no contrast to keep.
MIN_CONTRAST_SPREAD = 8.0

# ── The gate: what the audience would see, each a failing finding ──
#: The last raw mattes a median is taken over, frame to frame: the encoder's noise at the tolerance never flickers.
MATTE_WINDOW = 5
#: A non-key region inside a keyed surface is in front of it when it reaches the surface's rim (it came in from
#: outside), or holds at least this share of the quad (an object standing inside it); a smaller one closed inside the
#: surface is what the model drew on the screen, and is covered.
FOREGROUND_MIN_SHARE = 0.1
#: A pixel of the composed quad of the key's hue (:data:`HALO_HUE_DEGREES`) and saturated past this is residual key ...
RESIDUAL_MIN_SATURATION = 0.4
#: ... and a connected region of them at least this share of the quad (at least :data:`RESIDUAL_MIN_PIXELS`) counts ...
RESIDUAL_MIN_SHARE = 0.0005
RESIDUAL_MIN_PIXELS = 12
#: ... read inside the drawn quad and this share of the frame's width around it, no farther: past it, the
#: footage's own key colour - its screen's glow on the rim, a spill on the desk - is the footage's, never the insert's ...
RESIDUAL_MARGIN = 0.005
#: ... and a frame fails past this share of the quad: under it, a few pixels nobody sees, said in the insert's report.
RESIDUAL_FAIL_SHARE = 0.003
#: A straight run of the key's hue across the quad - a column down it, a row across it, of the key's hue on at least
#: this share of the quad's extent there ...
SEAM_RUN_SHARE = 0.8
#: ... at most this many pixels of a 1080p frame wide (a person standing in front is wider), on a quad at least this
#: many pixels across, is a seam of the compositing: the residual key counts it whatever mask it falls in, the source's
#: own key hue drawn at that place aside, and a frame with one fails whatever its share of the quad.
SEAM_MAX_PX = 3.0
SEAM_MIN_EXTENT_PX = 8
#: A line of the key's hue along an edge of the quad, read on a band this many pixels of a 1080p frame either side of
#: it (inside the quad and past it, over the bezel) ...
EDGE_LINE_BAND_PX = 2.0
#: ... at one offset from the edge on at least this share of the edge's length, is a line the key left there: the
#: residual key fails the frame whatever its share, naming the side and its width (:func:`key_edge_lines`).
EDGE_LINE_RUN_SHARE = 0.6
#: In the layer's margin past :data:`RESIDUAL_MARGIN`, a pixel of the key's hue saturated past this is despilled:
#: brought toward the luminance around it (fully from :data:`RESIDUAL_MIN_SATURATION`), in every frame ...
DESPILL_MIN_SATURATION = 0.25
#: ... that luminance read over the margin's pixels of another hue, a Gaussian of this many pixels of a 1080p frame.
DESPILL_SIGMA = 3.0
#: The matte's coverage of the quad, or the insert's luma against its source, reversing between consecutive frames
#: by more than this is flicker (``insert_checks``).
FLICKER_COVERAGE = 0.02
FLICKER_LUMA = 6.0
#: The Laplacian variance of the insert region at the output size stays at least this share of the source's at that
#: size. Measured on synthetic UI, photo and noise sources over three quads at 720p: the supersampled warp reads
#: 0.73-1.16, a bilinear read at the output size 0.11-0.57 on a source denser than its quad.
MIN_SHARPNESS = 0.5
#: Hue outliers (the key's hue, its complement) along the foreground's edge, as a share of that edge, fail past this.
MAX_HALO_SHARE = 0.05
#: An outlier is within this many degrees of the key's hue (or of its complement) and saturated past this (HSV).
HALO_HUE_DEGREES = 30.0
HALO_MIN_SATURATION = 0.3
# ── The key's residue on the edge of what stands in front ──
#: The part of what stands in front the key reads clear (:data:`KEY_CLEAR_MATTE`) is the subject; the band read on the
#: surface side of it runs from this many pixels of a 1080p frame ...
RIM_FROM_PX = 2.0
#: ... to this many: as far as the key's residue on a dark edge reaches, and as far from that part as the layer draws
#: the insert over that residue (:func:`key_residue`) ...
RIM_PX = 12.0
#: ... a pixel of the band grey (``screens.MIN_HUE_CHROMA``) where the insert is not, its luma between the subject's and
#: the insert's - or the key's own once clamped, the residue's - within this many grey levels, is rim ...
RIM_LUMA_MARGIN = 8.0
#: ... and a frame fails past this share of the band (``insert_rim``).
MAX_RIM_SHARE = 0.15
# ── What every gate of a frame reads, computed once (:class:`GateMasks`) ──
#: The source's own pixels of the key's hue (or of its complement) - a product UI's green chart, ring or bar - grown by
#: this many pixels of a 1080p frame: what the insert shows, never the key left, so the residual-key gate and the
#: halo's edge leave them out ...
SOURCE_HUE_GROW_PX = 2.0
#: ... and what stands in front of the surface, grown by this many pixels of a 1080p frame and the key's feather - its
#: edge ring blends it with the insert -, is left out of the residual key, the colour and the sharpness, and of the
#: bezel lock's and the flatness's edge readings (:func:`edge_front`).
FOREGROUND_GROW_PX = 3.0
#: What stands in front of a keyed surface's edges is read inside the surface's own outline (:func:`edge_front`):
#: the convex hull of its key regions holding at least this share of the largest one - a subject crossing the surface
#: in two keeps both halves, a key colour past the bezel is no part of it.
SURFACE_PART_SHARE = 0.25
#: A frame whose foreground covers more than this share of the quad leaves too little of the insert to hold its
#: colours to the source: not compared, said.
COLOUR_MAX_FOREGROUND = 0.3
#: What fills a contain fit's bands under the source (:data:`BAND_FILLS`): bands filled so are the fit's, never the key.
FILLED_BANDS = ("source edge", "bezel", "black")


def host_size(width: Any, height: Any) -> tuple[int, int] | None:
    """The size a frame is drawn at, from the page's request: whole pixels, at most :data:`MAX_SIDE`."""
    try:
        w, h = int(float(width)), int(float(height))
    except (TypeError, ValueError):
        return None
    if w < 1 or h < 1:
        return None
    scale = min(1.0, MAX_SIDE / max(w, h))
    return max(1, int(w * scale)), max(1, int(h * scale))


def bbox(quad: list[tuple[float, float]], width: int, height: int) -> tuple[int, int, int, int] | None:
    """The quad's bounding box in a host of *width* x *height* pixels, cut to the host: ``(x0, y0, x1, y1)``.

    The page computes the same box (``insertBox`` in ``compositor_page``):
    ``floor(min * size)`` and ``ceil(max * size)`` of the same numbers.
    ``None`` when the quad lies outside the host.
    """
    xs, ys = [point[0] for point in quad], [point[1] for point in quad]
    x0, x1 = max(0, math.floor(min(xs) * width)), min(width, math.ceil(max(xs) * width))
    y0, y1 = max(0, math.floor(min(ys) * height)), min(height, math.ceil(max(ys) * height))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


@dataclass(frozen=True)
class Warped:
    """One warped frame: the box it covers in the host, and its pixels (RGBA, straight alpha)."""

    box: tuple[int, int, int, int]
    image: Any


def shift_source(coefficients: list[float], dx: float, dy: float) -> list[float]:
    """Pillow perspective coefficients whose source coordinates are moved by ``(dx, dy)``."""
    a, b, c, d, e, f, g, h = coefficients
    return [a + dx * g, b + dx * h, c + dx, d + dy * g, e + dy * h, f + dy, g, h]


def warp(
    picture: Any,
    quad: list[tuple[float, float]],
    size: tuple[int, int],
    fit: str = "cover",
    *,
    within: list[tuple[float, float]] | None = None,
) -> Warped | None:
    """*picture* (a Pillow image) mapped onto *quad* in a host of *size* pixels; ``None`` when the quad is off it.

    *within* is the quad whose box the frame covers, when it is not *quad*'s own (a tracked insert's).
    """
    from PIL import Image

    width, height = size
    box = bbox(within or quad, width, height)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    corners = tuple((x * width - x0, y * height - y0) for x, y in quad)
    quad_w, quad_h = quad_size(corners)
    source = fitted(picture.convert("RGBA"), quad_w, quad_h, fit, pad=LETTERBOX)
    # The longest edges of the quad: where the picture is drawn largest, it is read one to one.
    tl, tr, br, bl = corners
    longest_w, longest_h = max(math.dist(tl, tr), math.dist(bl, br)), max(math.dist(tl, bl), math.dist(tr, br))
    scale = max(longest_w / source.width, longest_h / source.height)
    # Supersampled while the box fits the budget: read at twice the size and brought down once with LANCZOS,
    # the source's strokes keep their edges whether it is denser or sparser than the quad it is drawn into.
    ss = SUPERSAMPLE if (x1 - x0) * (y1 - y0) <= SUPERSAMPLE_BUDGET else 1
    if ss > 1:
        corners = tuple((x * ss, y * ss) for x, y in corners)
        scale *= ss
    # The source brought to the size it is drawn at (LANCZOS, reduced or enlarged), so the bilinear read of the
    # transform is about one source pixel per pixel drawn: it blurs nothing it did not have to.
    if abs(scale - 1.0) > RESIZE_TOLERANCE:
        resized = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        source = source.resize(resized, Image.Resampling.LANCZOS, reducing_gap=2.0)
    premultiplied = source.convert("RGBa")
    padded = Image.new("RGBa", (source.width + 2 * EDGE_PAD, source.height + 2 * EDGE_PAD), (0, 0, 0, 0))
    padded.paste(premultiplied, (EDGE_PAD, EDGE_PAD))
    coefficients = shift_source(perspective_coefficients(corners, source.width, source.height), EDGE_PAD, EDGE_PAD)
    # Read bilinear at twice the size and brought down with LANCZOS, the source's strokes keep their edges.
    drawn = padded.transform(
        ((x1 - x0) * ss, (y1 - y0) * ss),
        Image.Transform.PERSPECTIVE,
        coefficients,
        resample=Image.Resampling.BILINEAR,
        fillcolor=(0, 0, 0, 0),
    )
    if ss > 1:
        drawn = drawn.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    return Warped(box, drawn.convert("RGBA"))


def glow(
    warped: Warped,
    size: tuple[int, int],
    *,
    quad: list[tuple[float, float]] | None = None,
    tint: tuple[int, int, int] | None = None,
) -> Any:
    """The bloom of *warped* over the whole host at 1/:data:`GLOW_SCALE` of its size (RGBA, straight alpha).

    The silhouette of what is drawn - its alpha, so a transparent pixel adds
    no light and a mark halos its own shape - blurred, in one colour: *tint*
    (the insert's mean colour) at :data:`GLOW_ALPHA` at its brightest, never
    the blurred source itself, which read as a yellow-green halo on dark
    footage. Clipped to the bezel: *quad* (0-1 of the host) grown by
    :data:`GLOW_REACH` of its width, feathered, so the light never falls on
    the wall around the screen. Without *quad* and *tint*, the blurred source
    over the whole host.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    glow_w, glow_h = max(1, math.ceil(width / GLOW_SCALE)), max(1, math.ceil(height / GLOW_SCALE))
    x0, y0, x1, y1 = warped.box
    small = warped.image.convert("RGBa").resize(
        (max(1, round((x1 - x0) / GLOW_SCALE)), max(1, round((y1 - y0) / GLOW_SCALE))), Image.Resampling.BOX
    )
    layer = Image.new("RGBa", (glow_w, glow_h), (0, 0, 0, 0))
    layer.paste(small, (round(x0 / GLOW_SCALE), round(y0 / GLOW_SCALE)))
    radius = max(1.0, GLOW_RADIUS * min(glow_w, glow_h))
    bloom = layer.filter(ImageFilter.GaussianBlur(radius)).convert("RGBA")
    if quad is None or tint is None:
        return bloom
    alpha = np.asarray(bloom, dtype=np.float32)[..., 3] / 255.0
    corners = np.array([(x * width, y * height) for x, y in quad], dtype=float)
    quad_w, _quad_h = quad_size(tuple(map(tuple, corners)))
    centre = corners.mean(axis=0)
    away = corners - centre
    grown = corners + away / np.maximum(np.linalg.norm(away, axis=1, keepdims=True), 1e-6) * (GLOW_REACH * quad_w)
    clip = Image.new("L", (glow_w, glow_h), 0)
    ImageDraw.Draw(clip).polygon([(x / GLOW_SCALE, y / GLOW_SCALE) for x, y in grown], fill=255)
    feathered = np.asarray(clip.filter(ImageFilter.GaussianBlur(1.0)), dtype=np.float32) / 255.0
    shaped = np.clip(alpha * feathered * GLOW_ALPHA, 0.0, 1.0)
    colour = np.empty((glow_h, glow_w, 4), dtype=np.float32)
    colour[..., :3] = np.asarray(tint, dtype=np.float32)
    colour[..., 3] = shaped * 255.0
    return Image.fromarray(np.clip(colour, 0, 255).astype(np.uint8), "RGBA")


def mean_colour(shown: Any, alpha: Any) -> tuple[int, int, int] | None:
    """The mean colour of *shown* (``H x W x 3``) over the pixels *alpha* (0-1) draws: the tint of the insert's glow."""
    import numpy as np

    weight = np.asarray(alpha, dtype=np.float32)
    total = float(weight.sum())
    if total < 1.0:
        return None
    mean = (np.asarray(shown, dtype=np.float32) * weight[..., None]).sum(axis=(0, 1)) / total
    return int(mean[0]), int(mean[1]), int(mean[2])


def _halo(bloom: Any, box: tuple[int, int, int, int]) -> Any:
    """The light of *bloom* (:func:`glow`, RGBA at 1/:data:`GLOW_SCALE` of the host) over *box* of the host.

    ``H x W x 3`` float, 0-255, premultiplied: what a ``screen`` blend adds there at full strength.
    """
    import numpy as np
    from PIL import Image

    x0, y0, x1, y1 = box
    gx0, gy0 = x0 // GLOW_SCALE, y0 // GLOW_SCALE
    gx1, gy1 = min(bloom.width, math.ceil(x1 / GLOW_SCALE)), min(bloom.height, math.ceil(y1 / GLOW_SCALE))
    if gx1 <= gx0 or gy1 <= gy0:
        return np.zeros((y1 - y0, x1 - x0, 3), dtype=np.float32)
    cut = bloom.crop((gx0, gy0, gx1, gy1)).resize(((gx1 - gx0) * GLOW_SCALE, (gy1 - gy0) * GLOW_SCALE), Image.Resampling.BILINEAR)
    left, top = x0 - gx0 * GLOW_SCALE, y0 - gy0 * GLOW_SCALE
    padded = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    padded.paste(cut.crop((left, top, left + x1 - x0, top + y1 - y0)), (0, 0))
    light = np.asarray(padded, dtype=np.float32)
    return light[..., :3] * (light[..., 3:4] / 255.0)


def ambient_light(luma: Any, surface: Any, quad_px: Any) -> Any | None:
    """The footage's light over a box: ``H x W`` grey levels, 0 and up - its low frequencies above the surface's own level.

    *luma* is the footage's luma over the box, *surface* (``H x W``, 0-1)
    where it shows the surface the insert covers (inside the quad, clear of
    what stands in front), *quad_px* the quad in the box's pixels. The luma is
    averaged over the surface alone - a normalised blur, so the bezel and the
    foreground lend it nothing - at :data:`LIGHT_SIGMA` of the quad's short
    side, on a grid at most :data:`LIGHT_GRID` long, and taken above its
    :data:`LIGHT_FLOOR_PERCENTILE` over the surface: what is left is the light
    that varies over it - a reflection, a lamp's falloff, a window's glare.
    Luma alone: the surface's colour (the key's green, a screen the model
    drew, the room's blue) is never light, so it casts nothing on the insert.
    ``None`` with too little surface to read.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    weight = np.clip(np.asarray(surface, dtype=np.float32), 0.0, 1.0)
    height, width = weight.shape
    if float(weight.sum()) < 64.0:
        return None
    quad_w, quad_h = quad_size(tuple(map(tuple, quad_px)))
    scale = min(1.0, LIGHT_GRID / max(width, height))
    grid = (max(2, round(width * scale)), max(2, round(height * scale)))

    def reduced(values: Any) -> Any:
        return np.asarray(Image.fromarray(np.ascontiguousarray(values, dtype=np.float32), "F").resize(grid, Image.Resampling.BOX), dtype=np.float32)

    sigma = max(0.8, LIGHT_SIGMA * max(1.0, min(quad_w, quad_h)) * scale)
    share = ndimage.gaussian_filter(reduced(weight), sigma, mode="nearest")
    low = ndimage.gaussian_filter(reduced(np.asarray(luma, dtype=np.float32) * weight), sigma, mode="nearest") / np.maximum(share, 1e-4)
    seen = reduced(weight) > 0.5
    if int(seen.sum()) < 4:
        return None
    floor = float(np.percentile(low[seen], LIGHT_FLOOR_PERCENTILE))
    light = np.where(share > 1e-3, np.clip(low - floor, 0.0, 255.0), 0.0).astype(np.float32)
    return np.asarray(Image.fromarray(light, "F").resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)


def lay_light(shown: Any, light: Any, strength: float) -> Any:
    """*shown* (``H x W x 3`` float) with *light* (grey levels, :func:`ambient_light`) laid over it at *strength*.

    A screen blend, ``1 - (1 - a)(1 - b)``: light adds to what the insert
    shows and never past white, the same on every channel - a black lifts
    toward the light's level and takes no colour.
    """
    import numpy as np

    added = np.clip(np.asarray(light, dtype=np.float32) * float(strength) / 255.0, 0.0, 1.0)[..., None]
    return 255.0 - (255.0 - np.asarray(shown, dtype=np.float32)) * (1.0 - added)


def hue_name(degrees: float | None) -> str | None:
    """A hue's name (``spec.HUE_NAMES``): ``blue`` for 230 degrees; ``None`` for none."""
    if degrees is None:
        return None
    turned = float(degrees) % 360.0
    return next(name for bound, name in HUE_NAMES if turned < bound or bound == 360.0)


def own_hue_spans(own_hues: Any) -> tuple[tuple[float, float], ...]:
    """The hue spans (``(from, to)`` degrees, ``from < to``) the colours an author declares a subject's own cover
    (``despill.own_hues``): a name its span of the wheel (``spec.HUE_NAMES``), a ``#rrggbb`` its hue and
    :data:`OWN_HUE_DECLARED_DEGREES` either side; what names nothing covers nothing."""
    import colorsys

    spans: list[tuple[float, float]] = []
    for given in own_hues or ():
        text = str(given).strip().lower()
        start = 0.0
        for bound, name in HUE_NAMES:
            if name == text:
                spans.append((start, bound))
            start = bound
        if len(text) == 7 and text.startswith("#"):
            try:
                rgb = tuple(int(text[i : i + 2], 16) / 255.0 for i in (1, 3, 5))
            except ValueError:
                continue
            centre = colorsys.rgb_to_hsv(*rgb)[0] * 360.0
            low, high = centre - OWN_HUE_DECLARED_DEGREES, centre + OWN_HUE_DECLARED_DEGREES
            spans.append((max(low, 0.0), min(high, 360.0)))
            # Across 0 degrees, the span goes on at the far end of the wheel.
            if low < 0.0:
                spans.append((low + 360.0, 360.0))
            if high > 360.0:
                spans.append((0.0, high - 360.0))
    return tuple(spans)


def in_spans(degrees: Any, spans: Any) -> Any:
    """Which of *degrees* (hues, an array) fall in one of *spans* (:func:`own_hue_spans`)."""
    import numpy as np

    turned = np.asarray(degrees, dtype=np.float32) % 360.0
    found = np.zeros(turned.shape, dtype=bool)
    for low, high in spans or ():
        found |= (turned >= low) & (turned < high)
    return found


def colour_badness(black_lift: float | None, hue_rotation: float | None, cast: float | None) -> float:
    """How far a frame's colours are past their thresholds: the largest of each measure over its threshold; past 1 it fails."""
    return max(
        (black_lift or 0.0) / MAX_BLACK_LIFT,
        (hue_rotation or 0.0) / MAX_HUE_ROTATION_DEGREES,
        (cast or 0.0) / MAX_CAST,
    )


def colour_fidelity(seen: Any, source: Any, judged: Any) -> dict | None:
    """What compositing did to an insert's colours: *seen* (``H x W x 3`` float, the quad as composited) held to
    *source* (the source drawn into the same pixels) on the *judged* pixels.

    ``luma_shift`` - the mean luma's move, a share of the full scale, signed;
    ``black_lift`` - the source's darkest :data:`BLACK_SHARE` of pixels (its
    black level) as composited, less as the source has them, a share of the
    full scale; ``contrast`` - the composited luma's spread (5th to 95th
    percentile) over the source's (``None``: a flat source). The colour: a
    source with a hue of its own (``screens.MIN_HUE_CHROMA``) is judged by how
    far its mean hue turned (``hue_rotation``, degrees) and by its chroma's
    scale (``saturation``); one with none - a dark UI, a grey - has no hue to
    turn, and the chroma the compositing added is its cast (``cast``, grey
    levels of chroma; ``cast_hue`` its hue, degrees). ``None`` with too little
    judged.
    """
    import colorsys

    import numpy as np

    from app.services.video_studio import screens

    mask = np.asarray(judged, dtype=bool)
    if int(mask.sum()) < 64:
        return None
    drawn = np.asarray(seen, dtype=np.float32)[mask]
    own = np.asarray(source, dtype=np.float32)[mask]
    drawn_ycc, own_ycc = screens.to_ycc(drawn), screens.to_ycc(own)
    drawn_luma, own_luma = drawn_ycc[:, 0], own_ycc[:, 0]
    # Every percentile of a luma in one partition: the same values as one call each, at a third of the cost.
    own_black, own_low, own_high = (float(v) for v in np.percentile(own_luma, [BLACK_SHARE * 100.0, 5, 95]))
    dark = own_luma <= own_black
    out: dict[str, Any] = {
        "luma_shift": round(float(drawn_luma.mean() - own_luma.mean()) / 255.0, 4),
        "black_lift": round(float(drawn_luma[dark].mean() - own_luma[dark].mean()) / 255.0, 4),
        "hue_rotation": None,
        "saturation": None,
        "cast": None,
        "cast_hue": None,
        "contrast": None,
    }
    own_spread = own_high - own_low
    if own_spread >= MIN_CONTRAST_SPREAD:
        drawn_low, drawn_high = (float(v) for v in np.percentile(drawn_luma, [5, 95]))
        out["contrast"] = round((drawn_high - drawn_low) / own_spread, 3)
    own_mean, drawn_mean = own_ycc[:, 1:].mean(axis=0), drawn_ycc[:, 1:].mean(axis=0)
    own_chroma = float(np.hypot(*own_mean))
    if own_chroma >= screens.MIN_HUE_CHROMA:
        turned = math.degrees(math.atan2(drawn_mean[1], drawn_mean[0]) - math.atan2(own_mean[1], own_mean[0]))
        out["hue_rotation"] = round(abs((turned + 180.0) % 360.0 - 180.0), 2)
        own_saturation = float(np.hypot(own_ycc[:, 1], own_ycc[:, 2]).mean())
        out["saturation"] = round(float(np.hypot(drawn_ycc[:, 1], drawn_ycc[:, 2]).mean()) / max(own_saturation, 1e-3), 3)
        return out
    cb, cr = (float(v) for v in drawn_mean - own_mean)
    out["cast"] = round(math.hypot(cb, cr), 2)
    # The cast's direction as a colour: the chroma added, brought back to RGB around no luma.
    direction = [1.402 * cr, -0.344136 * cb - 0.714136 * cr, 1.772 * cb]
    low, span = min(direction), max(direction) - min(direction)
    if span > 1e-6:
        out["cast_hue"] = round(colorsys.rgb_to_hsv(*((v - low) / span for v in direction))[0] * 360.0, 1)
    return out


def png(image: Any) -> bytes:
    """*image* as a PNG, compressed lightly: a frame is encoded once and read once."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def decode(data: bytes) -> Any:
    """A frame or a picture's bytes as a Pillow image, loaded."""
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# ── What an insert is ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeySpec:
    """A screen insert's chroma key as the compositor reads it (``spec.InsertKey``)."""

    color: str = "green"
    tolerance: float = 0.3
    #: The matte's soft edge, in pixels of a 1080p frame.
    feather: float = 1.5
    #: A named key's colour as its surface shows it (:meth:`sampled`); ``None``: not read yet, or a ``#rrggbb``.
    rgb: tuple[int, int, int] | None = None
    #: The surface's spread around the key (``screens.key_spread``), read with the colour; ``None``: not read.
    spread: float | None = None

    def __post_init__(self) -> None:
        if self.rgb is not None and not isinstance(self.rgb, tuple):
            object.__setattr__(self, "rgb", tuple(int(channel) for channel in self.rgb))

    def key(self) -> Any:
        """The ``screens.Key`` it keys on: a named colour's shade its surface shows, else the name's own, or its ``#rrggbb``."""
        from app.services.video_studio import screens

        named = dict(screens.KEY_CANDIDATES)
        if self.color in named:
            return screens.Key(self.color, self.rgb or named[self.color])
        return screens.Key.from_hex(self.color, self.color)

    def matte_tolerance(self) -> float:
        """The tolerance the matte is read with: the author's, widened to the surface's spread."""
        from app.services.video_studio import screens

        return screens.spread_tolerance(self.tolerance, self.spread)

    def sampled(self, footage: Any, quad_px: Any) -> KeySpec:
        """This key read off the surface inside *quad_px* (host pixels) of *footage* (``H x W x 3``): its colour and spread.

        ``screens.sample_key``: a ``#rrggbb`` is keyed as written; a named
        colour the surface shows too little of keeps the name's own colour.
        The surface's spread around the key (``screens.key_spread``) widens
        the matte's tolerance for both.
        """
        import dataclasses

        from app.services.video_studio import screens

        named = dict(screens.KEY_CANDIDATES)
        corners = [tuple(p) for p in quad_px]
        if self.color not in named:
            return dataclasses.replace(self, spread=screens.key_spread(footage, corners, self.key()))
        found = screens.sample_key(footage, corners, screens.Key(self.color, named[self.color]))
        keyed = dataclasses.replace(self, rgb=found.rgb if found is not None else None)
        return dataclasses.replace(keyed, spread=screens.key_spread(footage, corners, keyed.key()))

    @classmethod
    def of(cls, key: Any) -> KeySpec | None:
        """The key of a spec's overlay (``spec.InsertKey``, or its dict), ``None`` for none."""
        if key is None:
            return None
        read = key.get if isinstance(key, dict) else lambda name, default=None: getattr(key, name, default)
        return cls(str(read("color", "green")), float(read("tolerance", 0.3)), float(read("feather", 1.5)))


@dataclass(frozen=True)
class Despill:
    """How a keyed insert takes the key's spill off the edge of what stands in front (``spec.InsertDespill``).

    With :attr:`edge`, the key's colour is limited out of that edge (:func:`edge_limit`), what the clamp still leaves
    of its hue takes its clean neighbours' colour (:func:`edge_despill`) and the insert's light wraps onto it
    (:func:`light_wrap`); without it, that edge is drawn as the footage shows it, the key's clamp alone taken out.
    """

    edge: bool = True
    #: How far past the foreground's boundary the limit reaches, in pixels of a 1080p frame - the key's feather added.
    width_px: float = EDGE_SPILL_PX
    strength: float = 1.0
    #: The colours the author says what stands in front holds (``spec.InsertDespill.own_hues``): hue names or
    #: ``#rrggbb``, never despilled off its edge nor counted by the halo gate (:func:`own_hue_spans`).
    own_hues: tuple[str, ...] = ()

    @classmethod
    def of(cls, despill: Any) -> Despill:
        """The despill an overlay's ``despill`` names (a ``spec.InsertDespill``, or its payload as a dict); the default
        for none."""
        if despill is None:
            return cls()
        read = despill.get if isinstance(despill, dict) else lambda name, default=None: getattr(despill, name, default)
        own = tuple(str(hue).strip().lower() for hue in read("own_hues", None) or ())
        return cls(bool(read("edge", True)), float(read("width_px", EDGE_SPILL_PX)), float(read("strength", 1.0)), own)

    def as_dict(self) -> dict:
        drawn = {"edge": self.edge, "width_px": self.width_px, "strength": self.strength}
        return {**drawn, "own_hues": list(self.own_hues)} if self.own_hues else drawn

    def spans(self) -> tuple[tuple[float, float], ...]:
        """The hue spans its own colours cover (:func:`own_hue_spans`)."""
        return own_hue_spans(self.own_hues)

    def reach(self, scale: float, feather: float) -> float:
        """How far the limit reaches, in pixels of the frame drawn: :attr:`width_px` at *scale* (the frame's height over
        1080) and the key's *feather* (pixels)."""
        return max(1.0, self.width_px * scale + feather)


@dataclass(frozen=True)
class Footage:
    """The footage an insert is mapped onto, as its matte reads it: a video's frames, or a picture (a still backdrop)."""

    path: str
    kind: str = "video"
    fps: float = 24.0
    #: The frame server's key of its frames (``clip/<scene>``): the decoder the scene's own page reads.
    key: str | None = None


@dataclass(frozen=True)
class Zoom:
    """A screen insert's camera push (``spec.InsertZoom``): seconds after it comes in, how far, toward where."""

    start: float
    end: float
    scale: float
    focus: tuple[float, float] = (0.5, 0.5)

    @classmethod
    def of(cls, zoom: Any) -> Zoom | None:
        """The push of an overlay's ``zoom`` (a ``spec.InsertZoom``, or its payload as a dict); ``None`` for none."""
        if zoom is None:
            return None
        read = zoom.get if isinstance(zoom, dict) else lambda name: getattr(zoom, name)
        fx, fy = read("focus") or (0.5, 0.5)
        return cls(float(read("start") or 0.0), float(read("end")), float(read("scale")), (float(fx), float(fy)))

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "scale": self.scale, "focus": list(self.focus)}

    def scale_at(self, seconds: float) -> float:
        """How large the source is drawn *seconds* after the insert comes in: 1 before the push, eased to its scale."""
        span = self.end - self.start
        x = 1.0 if span <= 0 else min(max((float(seconds) - self.start) / span, 0.0), 1.0)
        return 1.0 + (self.scale - 1.0) * x * x * (3.0 - 2.0 * x)

    def window(self, size: tuple[int, int], scale: float) -> tuple[float, float, float, float]:
        """The part of a *size* source shown at *scale*: ``(left, top, right, bottom)`` in its pixels, centred on the
        focus and kept inside the source."""
        width, height = size
        shown_w, shown_h = width / max(scale, 1.0), height / max(scale, 1.0)
        cx = min(max(self.focus[0] * width, shown_w / 2), width - shown_w / 2)
        cy = min(max(self.focus[1] * height, shown_h / 2), height - shown_h / 2)
        return (cx - shown_w / 2, cy - shown_h / 2, cx + shown_w / 2, cy + shown_h / 2)


#: A push this close to 1 draws the source as it is: nothing is cut.
_UNZOOMED = 1.001


def zoomed(picture: Any, zoom: Zoom | None, seconds: float | None) -> Any:
    """*picture* (a Pillow image) as *zoom* shows it *seconds* after the insert comes in: the part of it the
    push frames, at that part's own pixels, then fitted to the quad as the whole source would be.

    Cut before the fit, so every gate reads the picture drawn (its colours, its sharpness, a cover's cut), and at
    a sub-pixel window (``EXTENT``), so the push glides instead of stepping a pixel at a time.
    """
    if zoom is None or seconds is None:
        return picture
    scale = zoom.scale_at(seconds)
    if scale <= _UNZOOMED:
        return picture
    from PIL import Image

    box = zoom.window(picture.size, scale)
    size = (max(1, round(box[2] - box[0])), max(1, round(box[3] - box[1])))
    return picture.transform(size, Image.Transform.EXTENT, box, resample=Image.Resampling.BICUBIC)


@dataclass(frozen=True)
class InsertSource:
    """A screen insert as the render serves it: its media, its surface, how it fills it, glows and is matted.

    *kind* ``video`` is decoded frame by frame (the page asks for the frame of
    the source it needs); ``image`` is one picture.
    """

    path: str
    kind: str
    quad: tuple[tuple[float, float], ...]
    fps: float = 24.0
    fit: str = "cover"
    glow: float = 0.0
    #: How much of the footage's light and reflections play over it (:func:`ambient_light`): 0, none.
    light: float = INSERT_DEFAULT_LIGHT
    #: Its quad on every frame of its scene's footage (``insert_tracking.InsertTrack``); ``None``: static.
    track: Any = None
    #: Its chroma key; ``None``: the occlusion of what enters the surface through its rim.
    key: KeySpec | None = None
    #: What its matte reads; ``None``: nothing is known of the surface, the insert is drawn whole.
    footage: Footage | None = None
    #: The footage frame it comes in on: the surface's reference.
    first_frame: int = 0
    #: The footage frames its surface's clean reference is read on (:func:`plate_frames`): spread over the frames
    #: it is drawn on, their median takes out what passes in front; empty: the frame it comes in on alone.
    plate: tuple[int, ...] = ()
    #: The brand mark it shows (``logo``, ``logo_light``, ``logo_mark``), checked for in every frame.
    logo: str | None = None
    #: What the findings call it (``Scene cat-cape: its screen insert 0``).
    label: str = ""
    #: The box every frame of a matted insert covers (:func:`grown_box`, the payload's ``box``): room for the
    #: bezel lock and the key margin around the quad; ``None``: the quad's own box (an unmatted insert).
    box: tuple[tuple[float, float], ...] | None = None
    #: Its camera push (:func:`zoomed`); ``None``: drawn at its fitted size.
    zoom: Zoom | None = None
    #: How its key's spill is taken off the edge of what stands in front; a keyed insert's alone.
    despill: Despill = Despill()
    #: What stands in front of its keyed surface over its clip, by colour (:func:`subject_of`): read once - by
    #: the save for a static insert, by the render for the others -; ``None``: not read.
    subject: Subject | None = None
    #: The quads of the other inserts its scene draws on the same footage (0-1 corners): a wall's other panes, a
    #: second screen - their key is theirs, never this one's spilled past its quad (:func:`key_spill`).
    others: tuple[tuple[tuple[float, float], ...], ...] = ()

    def quads(self, footage_frame: int | None) -> tuple[list[tuple[float, float]], list[tuple[float, float]] | None]:
        """Its quad on *footage_frame*, and the quad whose box every frame covers (its :attr:`box`, a track's), or ``None``."""
        quad = list(self.track.quad_at(footage_frame)) if self.track is not None else list(self.quad)
        if self.box is not None:
            return quad, [tuple(p) for p in self.box]
        if self.track is not None:
            return quad, [tuple(p) for p in self.track.box_quad()]
        return quad, None


def grown_box(quad: Any, aspect: float, margin: float = BOX_MARGIN) -> list[tuple[float, float]]:
    """The box of *quad* (0-1 corners) grown by *margin* of the frame's width every way, as four corners.

    *aspect* is the frame's width over its height: the same margin in
    pixels down the frame. The render puts it on the payload (``box``) and
    on the source (``InsertSource.box``), so the page and the driver place
    every frame of a matted insert on one box, the quad free to be snapped
    to its bezel inside it. A tracked insert's is grown from the box of all
    its quads.
    """
    xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
    dx, dy = margin, margin * aspect
    x0, x1, y0, y1 = min(xs) - dx, max(xs) + dx, min(ys) - dy, max(ys) + dy
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ── The matte ─────────────────────────────────────────────────────


def _solve(outputs: Any, inputs: Any) -> list[float]:
    """Pillow ``PERSPECTIVE`` coefficients mapping each output point onto its input point (four each)."""
    import numpy as np

    rows, values = [], []
    for (x, y), (u, v) in zip(outputs, inputs, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        values.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.append(v)
    return [float(c) for c in np.linalg.solve(np.array(rows, dtype=float), np.array(values, dtype=float))]


def _grown(quad: Any, by: float) -> Any:
    import numpy as np

    corners = np.asarray(quad, dtype=float)
    centre = corners.mean(axis=0)
    return centre + (corners - centre) * (1.0 + by)


def _grown_px(quad: Any, by: float) -> Any:
    """*quad*'s corners (pixels) moved *by* pixels away from its centre along their diagonals: its edges about *by* out."""
    import numpy as np

    corners = np.asarray(quad, dtype=float)
    away = corners - corners.mean(axis=0)
    return corners + away / np.maximum(np.linalg.norm(away, axis=1, keepdims=True), 1e-6) * by


def _grid_size(quad_px: Any) -> tuple[int, int]:
    """The grid a surface is compared on: its quad's proportions, at most :data:`SURFACE_SIDE` long."""
    quad_w, quad_h = quad_size(tuple(map(tuple, _grown(quad_px, RIM))))
    scale = min(1.0, SURFACE_SIDE / max(quad_w, quad_h, 1.0))
    return max(8, round(quad_w * scale)), max(8, round(quad_h * scale))


def rectified(frame: Any, quad_px: Any, grid: tuple[int, int], *, rim: float = RIM, mode: str = "RGB") -> Any:
    """*frame* (an ``H x W x C`` array) read on the plane of *quad_px* grown by *rim*, into a *grid*-sized array."""
    import numpy as np
    from PIL import Image

    gw, gh = grid
    corners = [tuple(p) for p in _grown(quad_px, rim)]
    coefficients = _solve(((0, 0), (gw, 0), (gw, gh), (0, gh)), corners)
    image = Image.fromarray(np.ascontiguousarray(frame), mode)
    return np.asarray(image.transform((gw, gh), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BILINEAR))


def _rim_mask(grid: tuple[int, int], rim: float = RIM) -> Any:
    """On the grid of a quad grown by *rim*, the band past the quad's own edges: where an occluder comes in from."""
    import numpy as np

    gw, gh = grid
    # Grown by *rim* from its centre, the quad's own edges stand this share of the grid in from each side.
    inner = rim / (2 * (1 + rim))
    ys, xs = np.mgrid[0:gh, 0:gw]
    u, v = (xs + 0.5) / gw, (ys + 0.5) / gh
    return (u < inner) | (u > 1 - inner) | (v < inner) | (v > 1 - inner)


def _channel_max(pixels: Any) -> Any:
    """The largest of the last axis' three channels of *pixels* (``H x W x 3``), as ``pixels.max(axis=-1)`` reads it,
    without numpy's strided reduction over a length-3 axis: three element-wise maxima, the same values."""
    import numpy as np

    return np.maximum(np.maximum(pixels[..., 0], pixels[..., 1]), pixels[..., 2])


def _drift(reference: Any, current: Any, rim: Any) -> tuple[float, float]:
    """How far the surface in *current* (grey) sits from where *reference* has it, in grid pixels ``(dx, dy)``.

    The shift, within :data:`DRIFT_SEARCH` grid pixels, that leaves the
    fewest pixels of the *rim* changed - searched on a grid half as long:
    what moves in front of a part of the rim (a cat, a hand) leaves the
    surface where it is, a camera the quad does not follow moves it all. No
    shift wins over none by less than :data:`DRIFT_MIN_GAIN` of the rim:
    an occluder's edge pixels crossing the threshold decide nothing.
    """
    import numpy as np

    a, seen, ring = reference[::2, ::2], current[::2, ::2], rim[::2, ::2]
    if not ring.any():
        return 0.0, 0.0
    reach = DRIFT_SEARCH // 2
    height, width = a.shape
    padded = np.pad(a, reach, mode="edge")
    still = float((np.abs(seen - a)[ring] > CHANGE_MIN).mean())
    best, where = still, (0, 0)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            if (dx, dy) == (0, 0):
                continue
            moved = padded[reach - dy : reach - dy + height, reach - dx : reach - dx + width]
            changed = float((np.abs(seen - moved)[ring] > CHANGE_MIN).mean())
            # Ties go to the smaller shift: the surface stays where it was unless a shift explains the frame better.
            if changed < min(best - 1e-6, still - DRIFT_MIN_GAIN) or (
                where != (0, 0) and abs(changed - best) <= 1e-6 and abs(dx) + abs(dy) < sum(map(abs, where))
            ):
                best, where = changed, (dx, dy)
    return float(2 * where[0]), float(2 * where[1])


@dataclass
class Surface:
    """The surface of an insert as its first footage frame shows it: what the frames after are held to."""

    grid: tuple[int, int]
    reference: Any
    #: The luma the surface is lit at (a keyed surface's exposure: the despill's reference).
    luma: float = 0.0
    #: Where a subject stood still over the plate (boolean, the grid; :func:`still_subject`): taken out of the
    #: reference, and where a change on a frame enters from, as it does from the rim. ``None``: none.
    still: Any = None
    #: The reference brought to the box last read on (:meth:`_on_box`): ``(quad and box, pixels, reached)``.
    box_reference: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def of(cls, footage: Any, quad_px: Any) -> Surface:
        """The surface on *footage* (``H x W x 3`` uint8, the host's size) inside *quad_px* (host pixels)."""
        return cls.of_plate([(footage, quad_px)])

    @classmethod
    def of_plate(cls, readings: list[tuple[Any, Any]]) -> Surface:
        """The clean surface over *readings*: ``(footage, quad_px)`` pairs - the frame the insert comes in on
        first, then frames spread over the ones it is drawn on (:func:`plate_frames`), each on its own quad -, read on
        the first one's grid, each brought to its exposure, and their median taken pixel by pixel: what passes in front
        on some of them, a subject standing there when the insert comes in included, is no part of the surface. A frame
        whose surface drifted past :data:`MAX_DRIFT` off the first (a camera the quad does not follow) is left out."""
        import numpy as np
        from scipy import ndimage

        weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
        grid = _grid_size(readings[0][1])
        rim = _rim_mask(grid)
        planes = []
        for footage, quad_px in readings:
            plane = ndimage.gaussian_filter(rectified(footage, quad_px, grid).astype(np.float32), (0.8, 0.8, 0))
            if planes:
                if max(abs(v) for v in _drift(planes[0] @ weights, plane @ weights, rim)) > MAX_DRIFT:
                    continue
                gain = float(np.clip(np.median((planes[0] @ weights + 4.0) / (plane @ weights + 4.0)), 0.5, 2.0))
                plane = plane * gain
            planes.append(plane)
        reference = planes[0] if len(planes) < 3 else np.median(np.stack(planes), axis=0).astype(np.float32)
        still = still_subject(reference, rim)
        if still is not None:
            reference = _filled_reference(reference, still, grid, np.stack(planes) if len(planes) >= 2 else None)
        luma = float(np.median(reference[~rim] @ weights))
        return cls(grid, reference, luma, still)

    def occluded(self, footage: Any, quad_px: Any) -> Any:
        """On this surface's grid, what stands in front of it in *footage* (boolean): what changed and came in by the rim.

        A pixel changed when, at the closest of its 3x3 neighbours in the
        reference (a track a pixel off moves nothing), it is :data:`CHANGE_MIN`
        grey levels and :data:`CHANGE_SPREAD` median differences off, an
        exposure change of the whole surface taken out first. A connected
        change that reaches the rim entered the surface from outside it: in
        front. One that stays inside is what the surface itself shows. What is
        in front reaches on through the weaker change joined to it
        (:data:`WEAK_CHANGE_MIN`, :data:`WEAK_CHANGE_SPREAD`): a dark subject
        over a dark screen is told from the screen by less than the bright
        wall it crossed. A frame whose surface moved past :data:`MAX_DRIFT` off
        its reference (:func:`_drift`) holds no occluder: the quad no longer
        sits on it.
        """
        return self._occluder(footage, quad_px)[0]

    def _occluder(self, footage: Any, quad_px: Any) -> tuple[Any, float | None]:
        """:meth:`occluded`'s mask and how sure it is: the share of what stands in front inside the quad that
        differs from the reference past the strong threshold on its own - the rest is in front by its link to what
        crossed the rim. ``None``: nothing in front inside the quad."""
        read = self._occluding(footage, quad_px)
        return read.mask, read.confidence

    def _occluding(self, footage: Any, quad_px: Any) -> Occlusion:
        """:meth:`_occluder`'s reading, with what the frame was held to the reference at: what the frame's own
        resolution is read against (:meth:`_full`)."""
        import numpy as np
        from scipy import ndimage

        weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
        current = ndimage.gaussian_filter(rectified(footage, quad_px, self.grid).astype(np.float32), (0.8, 0.8, 0))
        reference = self.reference
        rim = _rim_mask(self.grid)
        nothing = np.zeros(current.shape[:2], dtype=bool)
        if max(abs(v) for v in _drift(reference @ weights, current @ weights, rim)) > MAX_DRIFT:
            return Occlusion(nothing, None)
        ratio = (current @ weights + 4.0) / (reference @ weights + 4.0)
        gain = float(np.clip(np.median(ratio), 0.5, 2.0))
        scaled = reference * gain
        best = None
        padded = np.pad(scaled, ((1, 1), (1, 1), (0, 0)), mode="edge")
        gh, gw = current.shape[:2]
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                distance = _channel_max(np.abs(current - padded[dy : dy + gh, dx : dx + gw]))
                best = distance if best is None else np.minimum(best, distance)
        spread = float(np.median(best))
        strong = best > max(CHANGE_MIN, CHANGE_SPREAD * spread)
        changed = ndimage.binary_opening(strong, iterations=1)
        labels, count = ndimage.label(changed)
        if count == 0:
            return Occlusion(nothing, None, gain, spread)
        sizes = ndimage.sum(changed, labels, range(1, count + 1))
        # A change comes in through the rim - or stands where a subject stood still over the plate (:attr:`still`):
        # a cat sat under the screen for the whole shot never crosses the rim on any frame.
        entry = rim & changed
        if self.still is not None:
            entry |= self.still & changed
        entering = set(np.unique(labels[entry]).tolist()) - {0}
        smallest = MIN_OCCLUDER_SHARE * changed.size
        kept = [index for index in entering if sizes[index - 1] >= smallest]
        if not kept:
            return Occlusion(nothing, None, gain, spread)
        seeds = np.isin(labels, kept)
        # Hysteresis: the weaker change joined to what came in through the rim is the same subject, never the
        # surface's own noise, which joins nothing - where it is as thick as a subject.
        weak = ndimage.binary_opening(best > max(WEAK_CHANGE_MIN, WEAK_CHANGE_SPREAD * spread), iterations=1) | seeds
        grown, _count = ndimage.label(weak)
        joined = np.isin(grown, np.unique(grown[seeds])) & ~seeds
        front = ndimage.binary_fill_holes(seeds | ndimage.binary_opening(joined, iterations=WEAK_MIN_THICKNESS))
        # One body: a subject read patchily - fur in motion blur over a dark UI - is closed over
        # :data:`FRONT_CLOSE_PX` grid pixels and its holes filled, so the insert is never drawn through it.
        front = ndimage.binary_fill_holes(ndimage.binary_closing(front, iterations=FRONT_CLOSE_PX)) | front
        inside = front & ~rim
        if not inside.any():
            return Occlusion(front, None, gain, spread)
        return Occlusion(front, round(float((inside & strong).sum()) / float(inside.sum()), 4), gain, spread)

    def front(self, footage: Any, quad_px: Any, box: tuple[int, int, int, int], feather: float, *, full: bool = True) -> Front:
        """What stands in front of the surface in *footage* over *box* of the host (:class:`Front`): the share of
        the surface showing (feathered), what is in front, and how sure that is. With *full* - an unkeyed insert's -
        the grid's mask brought to the box is read again at the frame's own resolution and closed there
        (:meth:`_full`) before anything is drawn: its edge is the subject's faintest fringe, so none of it shows the
        surface - the feather falls past it, never on a strand a pixel wide, and a window of these readings thresholded
        at a half keeps every pixel of it. A keyed insert's fringe is its key's share of each pixel, its edge despill's
        and its halo's to read: grown over by the mask, it would hide them."""
        import numpy as np
        from scipy import ndimage

        read = self._occluding(footage, quad_px)
        mapped = self.on_box(read.mask.astype(np.float32), quad_px, box)
        foreground = mapped > 0.5
        if not full:
            visible = 1.0 - mapped
            if feather > 0.05:
                visible = ndimage.gaussian_filter(visible, feather)
            return Front(np.clip(visible, 0.0, 1.0), foreground, read.confidence)
        if foreground.any():
            foreground = self._full(footage, quad_px, box, foreground, read)
        visible = 1.0 - foreground.astype(np.float32)
        if feather > 0.05:
            visible = np.where(foreground, 0.0, ndimage.gaussian_filter(visible, feather))
        return Front(np.clip(visible, 0.0, 1.0), foreground, read.confidence)

    def on_box(self, values: Any, quad_px: Any, box: tuple[int, int, int, int]) -> Any:
        """*values* (an ``H x W`` float array on this surface's grid) brought to *box* of the host on *quad_px*'s plane,
        bilinear, nothing past the grid: the grid's mask as the insert is drawn on it."""
        import numpy as np
        from PIL import Image

        gw, gh = self.grid
        x0, y0, x1, y1 = box
        corners = [tuple(p) for p in _grown(quad_px, RIM) - np.array([x0, y0], dtype=float)]
        coefficients = _solve(corners, ((0, 0), (gw, 0), (gw, gh), (0, gh)))
        image = Image.fromarray(np.ascontiguousarray(values, dtype=np.float32), "F")
        mapped = image.transform((x1 - x0, y1 - y0), Image.Transform.PERSPECTIVE, coefficients, resample=Image.Resampling.BILINEAR, fillcolor=0.0)
        return np.asarray(mapped, dtype=np.float32)

    def _on_box(self, quad_px: Any, box: tuple[int, int, int, int]) -> tuple[Any, Any]:
        """The clean reference brought to *box* (``H x W x 3``) and where the grid reaches it (boolean), kept for the
        last quad and box asked: a static insert maps it once."""
        import numpy as np

        asked = (tuple(round(float(v), 3) for v in np.asarray(quad_px, dtype=float).ravel()), tuple(box))
        if self.box_reference is not None and self.box_reference[0] == asked:
            return self.box_reference[1], self.box_reference[2]
        reference = np.dstack([self.on_box(self.reference[..., channel], quad_px, box) for channel in range(3)])
        reached = self.on_box(np.ones(self.reference.shape[:2], dtype=np.float32), quad_px, box) > 0.999
        self.box_reference = (asked, reference, reached)
        return reference, reached

    def _full(self, footage: Any, quad_px: Any, box: tuple[int, int, int, int], seed: Any, read: Occlusion) -> Any:
        """*seed* - the grid's mask brought to *box* (boolean) - read again at the frame's own resolution.

        Within :data:`FULL_BAND_GRID` grid pixels of its edge - the grid's own
        pixels leave it no surer -, a pixel of *footage* (the frame's light
        through :data:`FULL_SIGMA_PX`) that differs from the clean reference
        brought to the box (at the frame's exposure, *read*'s gain; at its
        closest within a pixel) by the weaker change of :meth:`occluded` - at
        *read*'s spread, the surface's own over the grid, never the fringe's -
        and is joined to *seed* is in front: a soft edge's fringe, a tuft finer
        than a grid pixel. The mask is then closed with a square kernel
        :data:`FRONT_FULL_CLOSE_PX` wide at 1080p (:func:`close_width`) and its
        holes filled: no gap of a pixel or two inside what stands in front lets
        a stroke of the source through. Read in the window around *seed*, the
        grid's reach alone.
        """
        import numpy as np
        from scipy import ndimage

        x0, y0, x1, y1 = box
        scale = footage.shape[0] / 1080.0
        quad_w, quad_h = quad_size(tuple(map(tuple, _grown(quad_px, RIM))))
        gw, gh = self.grid
        band = math.ceil(FULL_BAND_GRID * max(1.0, quad_w / gw, quad_h / gh)) + 1
        width = close_width(scale)
        window = _around(seed, band + width)
        if window is None:
            return seed
        reference, reached = self._on_box(quad_px, box)
        frame = np.asarray(footage[y0:y1, x0:x1], dtype=np.float32)[window]
        sigma = max(0.5, FULL_SIGMA_PX * scale)
        frame = ndimage.gaussian_filter(frame, (sigma, sigma, 0))
        clean = np.pad(reference[window] * read.gain, ((1, 1), (1, 1), (0, 0)), mode="edge")
        rows, cols = frame.shape[:2]
        distance = None
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                apart = _channel_max(np.abs(frame - clean[dy : dy + rows, dx : dx + cols]))
                distance = apart if distance is None else np.minimum(distance, apart)
        inner = seed[window]
        near = (ndimage.distance_transform_edt(~inner) <= band) & reached[window]
        weak = near & (distance > max(WEAK_CHANGE_MIN, WEAK_CHANGE_SPREAD * read.spread))
        labels, _count = ndimage.label(weak | inner)
        joined = np.isin(labels, np.unique(labels[inner]))
        kernel = np.ones((width, width), dtype=bool)
        closed = ndimage.binary_fill_holes(ndimage.binary_closing(joined, structure=kernel) | joined)
        out = np.array(seed, copy=True)
        out[window] |= closed
        return out

    def visible(self, footage: Any, quad_px: Any, box: tuple[int, int, int, int], feather: float, *, full: bool = True) -> tuple[Any, Any]:
        """``(visible, foreground)`` over *box* of the host: the share of the surface showing (feathered), and what is in front."""
        front = self.front(footage, quad_px, box, feather, full=full)
        return front.visible, front.foreground


@dataclass(frozen=True)
class Front:
    """What stands in front of an unkeyed surface on one footage frame, over the box (:meth:`Surface.front`)."""

    #: The share of the surface showing in each pixel (feathered), and what stands in front (boolean).
    visible: Any
    foreground: Any
    #: The share of what stands in front inside the quad its difference from the reference alone tells apart, past the
    #: strong threshold (``None``: nothing in front there): low, it is in front mostly by its link to what crossed the
    #: rim - a dark subject over a dark screen.
    confidence: float | None


@dataclass(frozen=True)
class Occlusion:
    """One footage frame's occlusion read on a surface's grid (:meth:`Surface._occluding`): what stands in front
    (boolean, the grid), how sure that is (:attr:`Front.confidence`), and what the frame was held to the reference at -
    its exposure (*gain*) and the median of its differences, the surface's own noise (*spread*) - what the frame's own
    resolution is read against (:meth:`Surface._full`)."""

    mask: Any
    confidence: float | None
    gain: float = 1.0
    spread: float = 0.0


def close_width(scale: float) -> int:
    """The width of the square kernel what stands in front is closed with at the frame's own resolution:
    :data:`FRONT_FULL_CLOSE_PX` at 1080p, scaled with the frame (*scale*: its height over 1080), odd, never under 3."""
    return max(3, 2 * math.floor(FRONT_FULL_CLOSE_PX * scale / 2.0) + 1)


def still_subject(reference: Any, rim: Any) -> Any | None:
    """Where a subject stood still in front of the surface over the whole plate (boolean, the grid), part of its
    median *reference*: inside the quad (``~rim``), the connected regions :data:`STILL_CHANGE_MIN` grey levels and
    :data:`STILL_CHANGE_SPREAD` median differences off the surface's own flat colour (the median of the quad), touching
    the quad's edge and holding :data:`MIN_OCCLUDER_SHARE` of it at least, grown by two grid pixels. ``None`` when
    nothing, or when they hold more than :data:`STILL_MAX_SHARE` of the quad: what the surface shows, not a subject."""
    import numpy as np
    from scipy import ndimage

    inside = ~rim
    surface = np.median(reference[inside].reshape(-1, 3), axis=0)
    distance = np.abs(reference - surface).max(axis=-1)
    spread = float(np.median(distance[inside]))
    strong = ndimage.binary_opening((distance > max(STILL_CHANGE_MIN, STILL_CHANGE_SPREAD * spread)) & inside, iterations=1)
    labels, count = ndimage.label(strong)
    if count == 0:
        return None
    edge = ndimage.binary_dilation(rim, iterations=1) & inside
    touching = set(np.unique(labels[edge & strong]).tolist()) - {0}
    sizes = ndimage.sum(strong, labels, range(1, count + 1))
    kept = [index for index in touching if sizes[index - 1] >= MIN_OCCLUDER_SHARE * inside.sum()]
    if not kept:
        return None
    still = ndimage.binary_fill_holes(np.isin(labels, kept)) & inside
    if still.sum() > STILL_MAX_SHARE * inside.sum():
        return None
    return ndimage.binary_dilation(still, iterations=2) & inside


def _filled_reference(reference: Any, still: Any, grid: tuple[int, int], planes: Any = None) -> Any:
    """*reference* with *still* filled with the surface as it is behind the subject: where a frame of *planes*
    (the plate's frames, ``P x H x W x 3``) shows that pixel clear of the subject - within :data:`STILL_CHANGE_MIN` of
    the surface's flat colour -, the clearest frame's own pixel; elsewhere (a subject that never moved off it), a
    normalised convolution of the surface around it, its radius an eighth of the grid. A subject that moves off its
    plate position mid-shot then uncovers the surface the reference already holds, and the occluder reads nothing in
    front there: no ghost of the subject stays drawn where it stood."""
    import numpy as np
    from scipy import ndimage

    sigma = max(grid) / 8
    keep = (~still).astype(np.float32)
    numerator = ndimage.gaussian_filter(reference * keep[..., None], (sigma, sigma, 0))
    denominator = ndimage.gaussian_filter(keep, sigma)[..., None]
    filled = np.array(reference, dtype=np.float32, copy=True)
    filled[still] = (numerator / np.maximum(denominator, 1e-3))[still]
    if planes is not None and len(planes) >= 2:
        # The surface's flat colour as :func:`still_subject` reads it: inside the quad, never the wall past its rim.
        surface = np.median(reference[~(still | _rim_mask(grid))].reshape(-1, 3), axis=0)
        distance = np.abs(np.asarray(planes, dtype=np.float32) - surface).max(axis=-1)
        clearest = distance.argmin(axis=0)
        clear = still & (distance.min(axis=0) <= STILL_CHANGE_MIN)
        if clear.any():
            own = np.take_along_axis(np.asarray(planes, dtype=np.float32), clearest[None, ..., None], axis=0)[0]
            filled[clear] = own[clear]
    return filled.astype(np.float32)


def plate_frames(first: int, last: int) -> tuple[int, ...]:
    """The footage frames an insert drawn from *first* to *last* reads its surface's clean reference on
    (:meth:`Surface.of_plate`): *first*, then :data:`PLATE_FRAMES` in all spread evenly to *last*, each once."""
    first, last = max(0, int(first)), max(0, int(last))
    if last <= first:
        return (first,)
    count = min(PLATE_FRAMES, last - first + 1)
    return tuple(dict.fromkeys(round(first + (last - first) * index / (count - 1)) for index in range(count)))


def _coverage(quad_px: Any, box: tuple[int, int, int, int], oversample: int = 4) -> Any:
    """The share of each pixel of *box* the quad covers (antialiased by *oversample*)."""
    import numpy as np
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    mask = Image.new("L", (w * oversample, h * oversample), 0)
    ImageDraw.Draw(mask).polygon([((x - x0) * oversample, (y - y0) * oversample) for x, y in quad_px], fill=255)
    return np.asarray(mask.resize((w, h), Image.Resampling.BOX), dtype=np.float32) / 255.0


# ── One frame ─────────────────────────────────────────────────────────────


@dataclass
class FrameAudit:
    """What one drawn frame of an insert shows of its matte: the evidence its findings are made of."""

    #: The share of the quad the insert is drawn on - past :data:`DRAWN_MIN_ALPHA` of it - where what stands in front is,
    #: its edge ring left out (:func:`front_core`; ``None``: no matte, nothing known).
    covered: float | None = None
    #: The quad's edge drawn as a line over what stands in front (:func:`edge_line`): the pixels the insert is drawn on
    #: there per pixel of the quad's outline (``None``: not keyed, nothing in front).
    edge_line: float | None = None
    #: How sure the mask of what stands in front is: the share of it inside the quad its cue alone tells from
    #: the surface - the difference from the reference past the strong threshold, the key read clear of the surface
    #: (:data:`KEY_CLEAR_MATTE`) - the rest in front by its link to what crossed the rim (``None``: nothing in front).
    front_confidence: float | None = None
    #: The share of the core of what stands in front - the part the key reads clear, keyed - where the drawing put the
    #: source's own strokes (:func:`through_share`; ``None``: nothing in front, or a frame partly shown).
    through: float | None = None
    #: Whether every pixel :func:`through_share` found lies on the edge of what stands in front (:func:`on_edge`;
    #: ``None``: not failing), and whether the frame was drawn with its keyed matte refined there (:func:`compose`,
    #: #10573; ``None``: not).
    through_edge: bool | None = None
    matte_refined: bool | None = None
    #: An unkeyed insert's mask past its subject (:func:`matte_read`): the share of its edge with the surface
    #: left showing inside it, the largest region of that (a share of the frame), the lag of its centroid behind the
    #: subject's (pixels of a 1080p frame). ``None``: not read.
    matte_halo: float | None = None
    matte_halo_region: float | None = None
    matte_lag: float | None = None
    #: The key's hue past the quad on the whole footage frame (:func:`key_spill`): the share of the frame its
    #: regions past :data:`SPILL_MIN_SHARE` hold, and the largest one's box, 0-1 of the frame (``None``: none, or not
    #: keyed).
    key_spill: float | None = None
    key_spill_box: list[float] | None = None
    #: ... the quad's side that region runs on from, joined to the quad, and how far, 0-1 of the frame
    #: (:attr:`Spill.joined`; ``None``: detached, or none).
    key_spill_side: str | None = None
    key_spill_to: float | None = None
    #: The share of a brand mark's own opaque pixels still showing (``None``: not a brand mark).
    logo_visible: float | None = None
    #: What the compositing did to the insert's colours (:func:`colour_fidelity`; ``None``: no footage, or not
    #: read): its mean luma's and its black level's moves (shares of the full scale), the degrees its hue turned and
    #: its chroma's scale (a source with a hue), the cast it took and that cast's hue (a source with none), and its
    #: contrast's scale.
    luma_shift: float | None = None
    black_lift: float | None = None
    hue_rotation: float | None = None
    saturation: float | None = None
    cast: float | None = None
    cast_hue: float | None = None
    contrast: float | None = None
    #: The size the frame was composited and measured at, ``[w, h]``: the render's resolution, or more under
    #: a camera's zoom - never a draft's when the render is not one.
    size: list[int] | None = None
    #: The given quad against the bezel found under it, mean over the corners, a share of the frame's width
    #: (``surface_refine``); ``None``: no bezel found in this frame, nothing measured.
    alignment: float | None = None
    #: ``True`` when the bezel lock's temporal prior rejected this frame's refinement for the median of the last
    #: frames (``surface_refine.Refiner``): the refiner caught an inner edge; ``None`` otherwise ...
    held: bool | None = None
    #: ... and the given quad's error against the bezel it rejected, which the gate reads with :attr:`alignment`: a
    #: bezel that really stepped is never hidden while the prior settles.
    alignment_found: float | None = None
    #: The quad the insert was drawn into, snapped to the bezel, 0-1 across and down the footage.
    refined: list[list[float]] | None = None
    #: A tracked insert's footage frame within ``insert_tracking.BLEND_FRAMES`` of a switch between the tracker and
    #: the bezel read (``InsertTrack.near_switch``): a quad step there is the track's, not the footage's (``None``: no
    #: switch near, or not tracked).
    track_switch: bool | None = None
    #: The given quad against the bezel side by side: ``{side: share of the frame's width}``, the mean of the
    #: side's two corners, for the sides whose edge was found (``None``: none).
    alignment_sides: dict[str, float] | None = None
    #: A static insert's lock against the bezel this very frame shows (:func:`_drift_of`): how far the bezel
    #: has moved from under the quad locked on the first frame, mean over the corners placed, a share of the frame's
    #: width - what the alignment gate reads on every frame, where the lock alone would read the first frame on all
    #: of them -, by side (``drift_sides``), and that bezel, 0-1 across and down the footage (``bezel_found``).
    #: ``None``: not a locked insert, or no bezel found on this frame.
    alignment_drift: float | None = None
    drift_sides: dict[str, float] | None = None
    bezel_found: list[list[float]] | None = None
    #: What each edge's points say of the surface (``surface_refine.Flatness``): how far they bow (a share of
    #: the edge), the share of them on their line, and how far they stand off one straight line (a share of the
    #: frame's width), in ``surface_refine.SIDES`` order (``None``: not read).
    edge_bows: list[float | None] | None = None
    edge_inliers: list[float | None] | None = None
    edge_residuals: list[float | None] | None = None
    #: ... and the share of each edge's readings clear of what stands in front: under
    #: ``surface_refine.MIN_EDGE_VISIBLE`` the edge is mostly covered, its bow not judged.
    edge_visible: list[float | None] | None = None
    #: The share of the drawn quad the insert covers once matted (what the foreground leaves it).
    coverage: float | None = None
    #: The mean luma of the quad as shown, and of the source frame drawn into it: a matte or a despill that flickers
    #: moves the first and not the second.
    luma: float | None = None
    source_luma: float | None = None
    #: The share of the quad (grown by :data:`RESIDUAL_MARGIN`) still showing the key in connected regions after
    #: compositing (``None``: not keyed) - the source's own key hue, the filled contain bands and what stands in front
    #: left out, a seam counted whatever mask it falls in ...
    residual_key: float | None = None
    #: ... of it, the share of the quad in straight thin runs of the key's hue down or across it
    #: (:func:`key_seams`): a seam of the compositing, which fails the frame whatever its share (``None``: none).
    residual_seam: float | None = None
    #: ... and the share of the quad in pockets of the key left inside what stands in front, the insert not drawn there
    #: (:func:`key_pockets`): the key's colour beside a subject, which fails the frame whatever its share.
    residual_pocket: float | None = None
    #: ... and the lines of the key's hue along the quad's edges, ``{side: width in pixels}`` (:func:`key_edge_lines`):
    #: a thin line along a side, which fails the frame whatever its share (``None``: none).
    residual_edge: dict[str, int] | None = None
    #: The share of the quad's area the layer's margin would have drawn over the quads of the other inserts of its
    #: scene (:func:`others_cut`), cut out of the layer: their surfaces are theirs (``None``: none reached).
    others_cut: float | None = None
    #: ... the source's own pixels of the key's hue, a share of the quad (:class:`GateMasks`): a fact.
    source_key_hue: float | None = None
    #: The share of the quad what stands in front covers (``None``: no matte) and, past
    #: :data:`COLOUR_MAX_FOREGROUND`, ``True``: the colours were not compared on this frame.
    foreground_share: float | None = None
    colour_skipped: bool | None = None
    #: The share of the quad the colour and the sharpness are judged on (:meth:`GateMasks.clean`): the source
    #: drawn opaque, clear of what stands in front and of the contain bands.
    judged_share: float | None = None
    #: Where that residual key sits, a share of the quad each: ``{region: {side: share}}``, the region one of
    #: :data:`RESIDUAL_REGIONS` and the side the quad's edge nearest it (``None``: none left).
    residual_where: dict[str, dict[str, float]] | None = None
    #: The key's colour the layer took out of the margin past the quad (:func:`_despill_margin`), a share of
    #: the quad's area, and by side: ``{side: share}`` (``None``: none there).
    despilled: float | None = None
    despilled_where: dict[str, float] | None = None
    #: The Laplacian variance of the insert region at the output size over the source's at that size.
    sharpness: float | None = None
    #: Hue outliers along the foreground's edge, a share of that edge (``None``: not keyed, or no foreground), and that
    #: edge's length in pixels, the stretches over the source's own key hue left out.
    halo: float | None = None
    halo_edge: int | None = None
    #: The share of that edge whose colour is the subject's own (:meth:`Edge.owned`) - left out of :attr:`halo`
    #: -, and the share of the foreground's edge the edge despill recoloured (``None``: nothing read).
    halo_own: float | None = None
    edge_despill: float | None = None
    #: Of :attr:`halo`, the share counted on pixels the compositor's clamp touched (the complement counts there
    #: alone), and the mean hue counted, in degrees (``None``: nothing read, or nothing counted).
    halo_clamped: float | None = None
    halo_hue: float | None = None
    #: The key's residue on the edge of what stands in front (:func:`_rim_share`): the share of the band
    #: :data:`RIM_FROM_PX` to :data:`RIM_PX` on the surface side of the part the key reads clear that is grey where the
    #: insert is not (``None``: not keyed, or nothing in front the key reads clear), that part's edge in pixels, and
    #: the share of the rim the footage's light laid over the insert made grey (``None``: no rim).
    rim: float | None = None
    rim_edge: int | None = None
    rim_light: float | None = None
    #: The same grey share read off the keyed surface - the footage's own surroundings the quad overlaps - and on the
    #: whole band: facts, never gated (``None``: not read, or under 64 pixels there).
    rim_surroundings: float | None = None
    rim_whole: float | None = None
    #: Of that band off the keyed surface, the share the compositing made grey - the footage had colour there, the
    #: frame drawn has none: a subject's fringe the key's clamp turned grey over the insert. Gated as :attr:`rim`
    #: is (``None``: not read, or under 64 pixels there).
    rim_off: float | None = None
    #: The share of the edge of what stands in front the edge limit corrected (:func:`edge_limit`; ``None``: no
    #: edge, or the insert's ``despill.edge`` off).
    edge_spill: float | None = None
    #: How much of the matte drawn holds from the frame before - one less the share of its pixels that toggled back
    #: over the last two frames (``screens.edge_hold``; ``None``: no matte read frame by frame - a frame
    #: drawn whole - or the first two frames).
    edge_hold: float | None = None
    #: The share of the source's content (``pictures.content_box``) a ``cover`` fit cuts away (``None``: not cut).
    cropped: float | None = None
    #: The share of the quad a contain fit leaves outside the source (``None``: no band - another fit, or a
    #: source of the quad's proportions), what filled it (:data:`BAND_FILLS`) and its colour (``#RRGGBB``; ``None``
    #: for the surface or the footage).
    band_share: float | None = None
    band_fill: str | None = None
    band_colour: str | None = None
    #: Bands left to the footage (an unkeyed insert) showing a chroma key colour (:func:`band_key_colour`): its name
    #: (:data:`screens.KEY_CANDIDATES`) and the share of the bands' pixels of it; ``None``: no band, bands filled, or
    #: the footage there is no key colour.
    band_key: str | None = None
    band_key_share: float | None = None
    #: How much of a keyed insert held on its surface shows on this frame (:func:`compose`'s *visibility*), the
    #: rest of the surface in the bezel's colour; ``None``: all of it. Under 1, what the insert itself looks like - its
    #: colours, its sharpness, its luma, a brand mark - is not judged: only the surface (the key, the bezel, the edge).
    visibility: float | None = None

    @property
    def covers_foreground(self) -> bool:
        return self.covered is not None and self.covered > MAX_COVERED_SHARE

    @property
    def draws_edge_line(self) -> bool:
        return self.edge_line is not None and self.edge_line > MAX_EDGE_LINE_SHARE

    @property
    def through_foreground(self) -> bool:
        return self.through is not None and self.through > MAX_THROUGH_SHARE

    @property
    def matte_haloed(self) -> bool:
        return (self.matte_halo is not None and self.matte_halo > MAX_MATTE_HALO_SHARE) or (
            self.matte_halo_region is not None and self.matte_halo_region > MAX_MATTE_HALO_REGION
        )

    @property
    def matte_lags(self) -> bool:
        return self.matte_lag is not None and self.matte_lag > MAX_MATTE_LAG_PX

    @property
    def ghosted(self) -> bool:
        return (
            self.front_confidence is not None
            and self.front_confidence < LOW_FRONT_CONFIDENCE
            and (self.foreground_share or 0.0) > GHOST_MIN_FOREGROUND
        )

    @property
    def spills_key(self) -> bool:
        return self.key_spill is not None and self.key_spill > SPILL_MIN_SHARE

    @property
    def logo_lost(self) -> bool:
        return self.logo_visible is not None and self.logo_visible < LOGO_MIN_VISIBLE

    @property
    def colour_shifted(self) -> bool:
        return colour_badness(self.black_lift, self.hue_rotation, self.cast) > 1.0

    @property
    def misaligned(self) -> bool:
        from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

        worst = max(
            (value for value in (self.alignment, self.alignment_found, self.alignment_drift) if value is not None), default=None
        )
        return worst is not None and worst > ALIGNMENT_MAX_SHARE

    @property
    def keyed_residue(self) -> bool:
        return (
            bool(self.residual_seam)
            or bool(self.residual_pocket)
            or bool(self.residual_edge)
            or (self.residual_key is not None and self.residual_key > RESIDUAL_FAIL_SHARE)
        )

    @property
    def soft(self) -> bool:
        return self.sharpness is not None and self.sharpness < MIN_SHARPNESS

    @property
    def haloed(self) -> bool:
        return self.halo is not None and self.halo > MAX_HALO_SHARE

    @property
    def rimmed(self) -> bool:
        return (self.rim is not None and self.rim > MAX_RIM_SHARE) or (
            self.rim_off is not None and self.rim_off > MAX_RIM_SHARE
        )

    @property
    def flickers(self) -> bool:
        from app.services.video_studio import screens

        return self.edge_hold is not None and self.edge_hold < screens.MIN_EDGE_HOLD

    @property
    def crops(self) -> bool:
        return self.cropped is not None and self.cropped > MAX_CONTENT_CUT

    @property
    def failing(self) -> bool:
        return (
            self.covers_foreground
            or self.draws_edge_line
            or self.through_foreground
            or self.matte_haloed
            or self.ghosted
            or self.spills_key
            or self.logo_lost
            or self.colour_shifted
            or self.misaligned
            or self.keyed_residue
            or self.soft
            or self.haloed
            or self.rimmed
            or self.flickers
            or self.crops
        )

    @classmethod
    def from_dict(cls, data: dict) -> FrameAudit:
        """The audit :meth:`as_dict` wrote (a frame served from ``insert_frames``): its fields, the rest unread."""
        known = {each.name for each in fields(cls)}
        return cls(**{name: value for name, value in (data or {}).items() if name in known})

    def as_dict(self) -> dict:
        """The audit as a segment keeps it (``compositor.FrameServer.take_audits``), ``None`` for what was not read."""
        return {
            "covered": self.covered,
            "edge_line": self.edge_line,
            "front_confidence": self.front_confidence,
            "through": self.through,
            "through_edge": self.through_edge,
            "matte_refined": self.matte_refined,
            "matte_halo": self.matte_halo,
            "matte_halo_region": self.matte_halo_region,
            "matte_lag": self.matte_lag,
            "key_spill": self.key_spill,
            "key_spill_box": self.key_spill_box,
            "key_spill_side": self.key_spill_side,
            "key_spill_to": self.key_spill_to,
            "logo_visible": self.logo_visible,
            "luma_shift": self.luma_shift,
            "black_lift": self.black_lift,
            "hue_rotation": self.hue_rotation,
            "saturation": self.saturation,
            "cast": self.cast,
            "cast_hue": self.cast_hue,
            "contrast": self.contrast,
            "size": self.size,
            "alignment": self.alignment,
            "held": self.held,
            "alignment_found": self.alignment_found,
            "refined": self.refined,
            "track_switch": self.track_switch,
            "alignment_sides": self.alignment_sides,
            "alignment_drift": self.alignment_drift,
            "drift_sides": self.drift_sides,
            "bezel_found": self.bezel_found,
            "edge_bows": self.edge_bows,
            "edge_inliers": self.edge_inliers,
            "edge_residuals": self.edge_residuals,
            "edge_visible": self.edge_visible,
            "coverage": self.coverage,
            "luma": self.luma,
            "source_luma": self.source_luma,
            "residual_key": self.residual_key,
            "residual_seam": self.residual_seam,
            "residual_pocket": self.residual_pocket,
            "residual_edge": self.residual_edge,
            "others_cut": self.others_cut,
            "residual_where": self.residual_where,
            "source_key_hue": self.source_key_hue,
            "foreground_share": self.foreground_share,
            "colour_skipped": self.colour_skipped,
            "judged_share": self.judged_share,
            "despilled": self.despilled,
            "despilled_where": self.despilled_where,
            "sharpness": self.sharpness,
            "halo": self.halo,
            "halo_edge": self.halo_edge,
            "halo_own": self.halo_own,
            "halo_clamped": self.halo_clamped,
            "halo_hue": self.halo_hue,
            "rim": self.rim,
            "rim_edge": self.rim_edge,
            "rim_light": self.rim_light,
            "rim_surroundings": self.rim_surroundings,
            "rim_whole": self.rim_whole,
            "rim_off": self.rim_off,
            "edge_despill": self.edge_despill,
            "edge_spill": self.edge_spill,
            "edge_hold": self.edge_hold,
            "cropped": self.cropped,
            "band_share": self.band_share,
            "band_fill": self.band_fill,
            "band_colour": self.band_colour,
            "band_key": self.band_key,
            "band_key_share": self.band_key_share,
            "visibility": self.visibility,
        }


@dataclass
class Matting:
    """The frame-to-frame state of a matted insert, per page: its bezel lock and the raw mattes around the frame.

    A frame's matte is the median of the raw mattes of the :data:`MATTE_WINDOW`
    footage frames centred on it (:meth:`smoothed`): the two before, read
    when they were drawn, and the two after, read ahead (the frame server
    decodes them first, ``compositor.MATTE_LOOKAHEAD``). The encoder's noise
    at the tolerance boundary flips a pixel on one frame and not the next,
    and the median holds it; a person walking in is in three frames of five
    the moment they are in the frame shown, so the matte never lags them.
    """

    refiner: Any = None
    #: The refiner that reads a locked insert's bezel on every frame against its lock (:func:`_drift_of`), the
    #: bezel found carried as the next frame's seed; made on the first frame it reads.
    drift: Any = None
    #: Raw mattes by footage frame, the frames behind the window dropped.
    raw: dict[int, Any] = field(default_factory=dict)
    #: How sure each footage frame's occlusion is (:attr:`Front.confidence`), read with its raw matte.
    confidence: dict[int, float | None] = field(default_factory=dict)
    #: The source's own sharpness at the size it is drawn (:func:`_sharpness`), by picture and size: one entry.
    reference: dict[tuple, float] = field(default_factory=dict)
    #: The last two mattes drawn, as masks past a half, each with the frame it was drawn for: what the next
    #: frame's is held against (``FrameAudit.edge_hold``, ``screens.edge_hold``) when the three run in a row.
    last: tuple[int, Any] | None = None
    before: tuple[int, Any] | None = None
    #: A still source's last warp (:func:`warp`): ``((picture, quad, size, fit, box quad), Warped)``. The same picture
    #: drawn into the same quad is the same pixels on every frame.
    warped: tuple[tuple, Any] | None = None

    def __post_init__(self) -> None:
        from app.services.video_studio.surface_refine import Refiner

        if self.refiner is None:
            self.refiner = Refiner()

    def reading(self, frame: int | None, read: Any, *, fresh: bool = False) -> Any:
        """Footage frame *frame*'s raw matte (``H x W`` float), read by *read* once: a frame read ahead or behind for
        another frame's window is not read again when it is shown. *fresh* reads it anyway and keeps that reading: a
        tracked insert reads each frame on its own quad, never on the quad of the frame that read it ahead."""
        import numpy as np

        if frame is not None and not fresh:
            kept = self.raw.get(frame)
            if kept is not None:
                return kept
        now = np.asarray(read(), dtype=np.float32)
        if frame is not None:
            self.raw[frame] = now
        return now

    def warp_of(self, picture: Any, quad: list[tuple[float, float]], size: tuple[int, int], fit: str, within: Any) -> Any:
        """:func:`warp` of *picture* into *quad*, the last one drawn again when the picture and the quad are the same
        objects and values: a picture insert on a locked bezel warps once, not once a frame."""
        asked = (tuple(map(tuple, quad)), tuple(size), fit, tuple(map(tuple, within)))
        if self.warped is not None and self.warped[0][0] is picture and self.warped[0][1:] == asked:
            return self.warped[1]
        drawn = warp(picture, quad, size, fit, within=within)
        self.warped = ((picture, *asked), drawn)
        return drawn

    def hold(self, frame: int | None, matte: Any) -> float | None:
        """How much of *matte* (as drawn for *frame*) holds from the frame before (``screens.edge_hold``); ``None`` without two.

        Under ``screens.MIN_EDGE_HOLD`` the key blinks at its edge. A still, or a frame that does not follow
        the last two drawn in a row, is not compared.
        """
        import numpy as np

        from app.services.video_studio import screens

        mask = np.asarray(matte) > 0.5
        last, before = self.last, self.before
        self.last, self.before = ((frame, mask) if frame is not None else None), last
        if frame is None or last is None or before is None or last[0] != frame - 1 or before[0] != frame - 2:
            return None
        return screens.edge_hold(mask, last[1], before[1])

    def smoothed(self, frame: int | None, raw: Any, ahead: list[tuple[int, Any]] = ()) -> Any:
        """*raw* (footage frame *frame*'s matte, ``H x W`` float) as the median of the window around it.

        *ahead* gives the other frames of its window as ``(frame, read)``
        pairs - the frames after it, and the frames before it this state has
        not read (a page that starts elsewhere, or whose last frames were
        served already drawn) -, *read* a callable making that frame's raw
        matte: called once, the matte kept for when the frame is shown. A
        still (*frame* ``None``) has nothing to smooth over.
        """
        import numpy as np

        now = np.asarray(raw, dtype=np.float32)
        if frame is None:
            return now
        half = MATTE_WINDOW // 2
        for old in [n for n, kept in self.raw.items() if n < frame - half or kept.shape != now.shape]:
            self.raw.pop(old)
            self.confidence.pop(old, None)
        self.raw[frame] = now
        for other, read in ahead:
            if other not in self.raw and other != frame and frame - half <= other <= frame + half:
                self.raw[other] = np.asarray(read(), dtype=np.float32)
        window = [self.raw[n] for n in range(frame - half, frame + half + 1) if n in self.raw]
        if len(window) < 3:
            return now
        # The lower median: over an even count (a footage frame skipped by the time map, the window's edge) the two
        # middle values are never averaged into a half-drawn insert, and what stands in front wins the tie.
        return np.sort(np.stack(window), axis=0)[(len(window) - 1) // 2]


def _locked(
    region: Any,
    quad_px: Any,
    origin: Any,
    width: int,
    key: Any,
    tolerance: float | None,
    refiner: Any,
    hidden: Any = None,
    *,
    prior: bool = True,
    steady: bool = False,
    feather: float = 0.0,
    scale: float = 1.0,
    raw_matte: Any = None,
) -> tuple[Any, Any, float | None, Any, Any]:
    """The quad snapped to the bezel the frame shows (``surface_refine``): ``(drawn, refined, seed error, raw
    matte, found)`` - *found* the ``surface_refine.Refined``, which says whether the temporal prior (*prior*: an
    untracked quad) kept the lock of the last frames, and the error of the bezel it rejected; *steady* (a tracked
    quad) low-passes the correction drawn (``Refiner.step``), so the bezel read's frame-to-frame noise never steps it.

    A keyed surface's edge is its matte's (``screens.key_matte``, *raw_matte*
    when it was read already), another's
    its luma's; *hidden* marks what stands in front (its readings give no
    edge), and on a keyed surface so does what its matte says stands in front
    of the edges (:func:`edge_front`, the key's *feather* in pixels,
    *scale* the frame's height over 1080). The edge is found mid-ramp and
    drawn :data:`EDGE_GROW_PX` past it, so the insert's own antialiased edge
    covers the outer half of the surface's blurred edge and no rim of the key
    is left along the bezel.
    """
    import numpy as np

    from app.services.video_studio import screens, surface_refine
    from app.services.video_studio.tracking import luma as grey_of

    if key is not None:
        if raw_matte is None:
            raw_matte = screens.key_matte(region, key, tolerance)
        field = np.ascontiguousarray(raw_matte * 255.0, dtype=np.float32)
        band = max(surface_refine.MIN_BAND_PX, surface_refine.BAND * width)
        front = edge_front(raw_matte, quad_px - origin, band, scale, feather)
        if front is not None:
            hidden = front if hidden is None else (np.asarray(hidden, dtype=bool) | front)
    else:
        field = np.ascontiguousarray(grey_of(region), dtype=np.float32)
    found = refiner.step(
        field,
        quad_px - origin,
        width,
        hidden if hidden is not None and hidden.any() else None,
        prior=prior,
        steady=steady,
    )
    found_px = np.asarray(found.quad, dtype=float) + origin
    drawn_px = _grown_px(found_px, EDGE_GROW_PX) if found.found else found_px
    return drawn_px, found_px, found.seed_error, raw_matte, found


def _drift_of(
    region: Any,
    locked_px: Any,
    origin: Any,
    width: int,
    refiner: Any,
    hidden: Any = None,
    *,
    key: Any = None,
    tolerance: float | None = None,
    matte: Any = None,
) -> Any:
    """The bezel *region* (``H x W x 3`` float32, the box's footage) shows, read against *locked_px* - the bezel a
    static insert was locked on, in host pixels - as a tracker reads it (``surface_refine.Refiner.step`` with no
    prior: the lock is the seed, the bezel found is carried to the next frame's seed): ``(bezel found in host pixels,
    its distance from the lock as a share of the frame's width, the ``surface_refine.Refined``)``, or ``None`` each
    when no bezel is found on this frame. A keyed surface's edge is its matte's (*key*, *tolerance*; *matte* when it
    was read already), another's its luma's, as the lock read it; *hidden* marks what stands in front (its readings
    give no edge).

    Nothing is drawn by it: the insert stays on its lock, as it always did. What it measures is what the lock alone
    cannot see - a locked-off shot whose camera drifted a few percent over its seconds, a surface the model moved -,
    read on every frame so the alignment gate judges every frame and not the first one on all of them.
    """
    import numpy as np

    from app.services.video_studio import screens
    from app.services.video_studio.tracking import luma as grey_of

    if key is not None:
        keyed = matte if matte is not None else screens.key_matte(region, key, tolerance)
        field = np.ascontiguousarray(keyed * 255.0, dtype=np.float32)
    else:
        field = np.ascontiguousarray(grey_of(region), dtype=np.float32)
    excluded = hidden if hidden is not None and np.asarray(hidden).any() else None
    found = refiner.step(field, np.asarray(locked_px, dtype=float) - origin, width, excluded, prior=False)
    if not found.found or found.seed_error is None:
        return None, None, found
    return np.asarray(found.quad, dtype=float) + origin, float(found.seed_error), found


def _sides(points: Any, quad_px: Any) -> Any:
    """The index in :data:`SIDES` of the edge of *quad_px* nearest each of *points* (``N x 2``, the quad's pixels)."""
    import numpy as np

    corners = np.asarray(quad_px, dtype=float)
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    distances = []
    for start, end in zip(corners, np.roll(corners, -1, axis=0), strict=True):
        along = end - start
        t = np.clip(((points - start) @ along) / max(float(along @ along), 1e-9), 0.0, 1.0)
        distances.append(np.linalg.norm(points - (start + t[:, None] * along), axis=1))
    return np.argmin(np.stack(distances, axis=1), axis=1)


@dataclass
class Band:
    """The margin around a keyed insert's quad, read on one footage frame: what of the key lies past the quad.

    The key's colour in the margin is one of two things. Joined to the surface inside the quad, it is the surface
    running past a quad too small (:attr:`edge`): left as it is, for the residual-key gate to see. Parted from the
    surface by the bezel, it is the footage's own key colour past the bezel - a keyed panel the model drew larger
    than the bezel the quad is snapped to -, which no quad can cover without leaving the bezel: the insert's key
    takes it at its tolerance, feathered by its feather (:attr:`beyond`), and the bezel's colour on that side fills it
    (:attr:`bezel`).
    """

    #: The key's share of each pixel of the box past the bezel, feathered outward (0 elsewhere).
    beyond: Any
    #: The margin pixels of the surface running past the quad (boolean).
    edge: Any
    #: ``H x W x 3`` float: the bezel's colour on the side of each pixel :attr:`beyond` fills; ``None`` for none.
    bezel: Any | None = None

    def fill(self, colours: Any, share: Any = None) -> Any:
        """*colours* (``H x W x 3`` over the box) with the key past the bezel filled with the bezel's colour.

        *share* (``H x W``, 0-1) scales how much of it is filled: what stands in front of the surface keeps its own.
        """
        if self.bezel is None:
            return colours
        taken = self.beyond if share is None else self.beyond * share
        return colours * (1.0 - taken)[..., None] + self.bezel * taken[..., None]


def band_of(region: Any, matte: Any, inside: Any, distance: Any, margin: float, quad_px: Any, feather: float, sides: Any = None) -> Band:
    """The :class:`Band` of a keyed insert on *region* (``H x W x 3`` float over its box), its key's *matte* there.

    *inside* is the drawn quad's pixels (boolean), *distance* each pixel's distance to them, *margin* the margin's
    width and *quad_px* the drawn quad, all in the box's pixels; *feather* the key's soft edge in those pixels;
    *sides* the bezel's colour on each side (:func:`bezel_sides`: the one the residual margin is filled with),
    else read here on the margin's pixels clear of the key.
    """
    import numpy as np
    from scipy import ndimage

    outside = (distance <= margin) & ~inside
    nothing = np.zeros(inside.shape, dtype=np.float32)
    keyed = (np.asarray(matte) >= BAND_KEY_MATTE) & outside
    if not keyed.any():
        return Band(nothing, np.zeros(inside.shape, dtype=bool))
    ring = inside & ~ndimage.binary_erosion(inside, iterations=2)
    surface = (np.asarray(matte) >= BAND_KEY_MATTE) & ring
    labels, _count = ndimage.label(keyed | surface)
    joined = sorted(set(np.unique(labels[surface]).tolist()) - {0})
    edge = np.isin(labels, joined) & outside if joined else np.zeros(inside.shape, dtype=bool)
    core = keyed & ~edge
    if not core.any():
        return Band(nothing, edge)
    # The key's own soft edge around it, never the surface's: the matte where the core reaches, feathered outward.
    reach = ndimage.binary_dilation(core, iterations=2) & outside & ~ndimage.binary_dilation(edge, iterations=1)
    share = np.where(reach, np.asarray(matte, dtype=np.float32), 0.0).astype(np.float32)
    if feather > 0.05:
        share = np.maximum(share, np.clip(ndimage.gaussian_filter(share, feather), 0.0, 1.0))
    share = np.where(outside & ~edge, share, 0.0).astype(np.float32)
    if sides is not None:
        by_side = np.asarray(sides, dtype=np.float32)
    else:
        # The bezel's colour per side: the margin's pixels that are not the key, the nearest ones to the quad.
        clear = outside & (np.asarray(matte) < 0.1) & ~reach
        near_clear = clear & (distance <= max(2.0, margin * BEZEL_SAMPLE_SHARE))
        sample = near_clear if int(near_clear.sum()) >= 16 else clear
        if int(sample.sum()) < 16:
            return Band(nothing, edge)
        ys, xs = np.nonzero(sample)
        stride = max(1, len(ys) // 20000)
        ys, xs = ys[::stride], xs[::stride]
        colours = region[ys, xs].astype(np.float32)
        found = _sides(np.stack([xs + 0.5, ys + 0.5], axis=1), quad_px)
        overall = np.median(colours, axis=0)
        by_side = np.stack([np.median(colours[found == s], axis=0) if int((found == s).sum()) >= 16 else overall for s in range(len(SIDES))])
    bezel = np.zeros(region.shape, dtype=np.float32)
    ty, tx = np.nonzero(share > 0)
    bezel[ty, tx] = by_side[_sides(np.stack([tx + 0.5, ty + 0.5], axis=1), quad_px)]
    return Band(share, edge, bezel)


def bezel_colour(region: Any, matte: Any, inside: Any, distance: Any, margin: float, key: Any) -> tuple[float, float, float] | None:
    """The bezel's colour around a keyed quad: the median of the margin's pixels nearest the quad that are not
    the key (as :func:`band_of` reads it per side), its key's spill clamped out; ``None`` with too few to read.

    *region*, *matte*, *inside* and *distance* are over the box, as :func:`band_of` takes them.
    """
    import numpy as np

    from app.services.video_studio import screens

    clear = ~np.asarray(inside, dtype=bool) & (np.asarray(matte) < 0.1) & (distance <= max(2.0, margin * BEZEL_SAMPLE_SHARE))
    if int(clear.sum()) < 16:
        return None
    ys, xs = np.nonzero(clear)
    stride = max(1, len(ys) // 20000)
    median = np.median(np.asarray(region, dtype=np.float32)[ys[::stride], xs[::stride]], axis=0)
    clamped = screens.despill_clamp(median[None, None, :], key)[0, 0]
    return float(clamped[0]), float(clamped[1]), float(clamped[2])


def bezel_sides(region: Any, matte: Any, inside: Any, distance: Any, margin: float, reach: float, quad_px: Any, key: Any) -> Any | None:
    """The bezel's colour on each side of a keyed quad: ``4 x 3`` float in :data:`SIDES` order - the median of
    the pixels within *reach* of the quad on that side that are not the key (under :data:`BAND_KEY_MATTE`: a bezel the
    surface's light tints is still the bezel; the margin's, *margin* wide, when too few are; the whole ring's for a
    side too few read), its spill clamped out, a colour of the key's hue taken as its grey; ``None`` with too few.

    *region*, *matte*, *inside* and *distance* are over the box, as :func:`band_of` takes them; *quad_px* the drawn
    quad in the box's pixels.
    """
    import numpy as np

    from app.services.video_studio import screens

    clear = ~np.asarray(inside, dtype=bool) & (np.asarray(matte) < BAND_KEY_MATTE) & (distance <= margin)
    near = clear & (distance <= max(2.0, reach))
    sample = near if int(near.sum()) >= 16 else clear
    if int(sample.sum()) < 16:
        return None
    ys, xs = np.nonzero(sample)
    stride = max(1, len(ys) // 20000)
    ys, xs = ys[::stride], xs[::stride]
    colours = np.asarray(region, dtype=np.float32)[ys, xs]
    sides = _sides(np.stack([xs + 0.5, ys + 0.5], axis=1), quad_px)
    overall = np.median(colours, axis=0)
    by_side = np.stack([np.median(colours[sides == s], axis=0) if int((sides == s).sum()) >= 16 else overall for s in range(len(SIDES))])
    clamped = screens.despill_clamp(by_side[None, :, :], key)[0]
    grey = clamped @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    for side in range(len(SIDES)):
        if _keyish(clamped[side], key):
            clamped[side] = grey[side]
    return clamped.astype(np.float32)


def fill_margin(rgb: Any, key: Any, ring: Any, sides: Any, quad_px: Any, scale: float) -> tuple[Any, Any]:
    """*rgb* (``H x W x 3`` float, the layer over its box) with the key's hue in *ring* - the residual margin past the
    quad - replaced by the bezel's colour on its side (*sides*: :func:`bezel_sides`), and the pixels replaced.

    Never despilled toward a grey: the margin shows the bezel the quad is snapped to. With no bezel read (*sides*
    ``None``), the key's hue there is despilled as past the margin (:func:`_despill_margin`).
    """
    import numpy as np

    hued = _key_hued(rgb, key, ring)
    if not hued.any():
        return rgb, hued
    if sides is None:
        return _despill_margin(rgb, key, hued, scale), hued
    ys, xs = np.nonzero(hued)
    out = np.array(rgb, dtype=np.float32, copy=True)
    out[ys, xs] = np.asarray(sides, dtype=np.float32)[_sides(np.stack([xs + 0.5, ys + 0.5], axis=1), quad_px)]
    return out, hued


#: Another insert's quad overlapping this one's on more than this share of its own area is the same surface (two inserts
#: back to back on one screen): never cut out of this layer.
OTHERS_SAME_SURFACE_SHARE = 0.05


def others_cut(others: Any, inside: Any, box: tuple[int, int, int, int], size: tuple[int, int]) -> Any:
    """Where a keyed layer over *box* must not draw (boolean over the box, ``None``: nowhere): the quads *others*
    (0-1 corners, ``InsertSource.others``) of the other inserts of its scene, outside its own quad *inside* (boolean
    over the box). A quad sharing more than :data:`OTHERS_SAME_SURFACE_SHARE` of its area with this one is the same
    surface and left out."""
    import numpy as np

    if not others:
        return None
    width, height = size
    own = np.asarray(inside, dtype=bool)
    out = np.zeros(own.shape, dtype=bool)
    for quad in others:
        cover = _coverage(np.array([(x * width, y * height) for x, y in quad], dtype=float), box)
        area = float(cover.sum())
        if area <= 0.0:
            continue
        if float(cover[own].sum()) > OTHERS_SAME_SURFACE_SHARE * area:
            continue
        out |= cover > 0.0
    out &= ~own
    return out if out.any() else None


def running_past(edge: Any, distance: Any, reach: float) -> Any:
    """The pixels of *edge* (``Band.edge``: the surface joined to the quad's) in regions reaching past *reach* from the
    quad: the surface running past a quad too small, which no margin hides - left for the residual-key gate.
    A bleed of the surface within *reach* is not."""
    import numpy as np
    from scipy import ndimage

    joined = np.asarray(edge, dtype=bool)
    if not joined.any():
        return joined
    labels, _count = ndimage.label(joined)
    far = np.unique(labels[joined & (distance > reach + 1.0)])
    return np.isin(labels, far[far > 0])


@dataclass(frozen=True)
class GateMasks:
    """What every gate of one drawn frame reads, computed once (:func:`gate_masks`): booleans over the box."""

    #: The source's own pixels of the key's hue, and of its complement, grown by :data:`SOURCE_HUE_GROW_PX`: what the
    #: insert shows - the residual key and the halo's edge leave them out.
    source_keyhue: Any
    source_complement: Any
    #: The bands a contain fit leaves inside the quad, filled under the source (:data:`FILLED_BANDS`); ``None``: none.
    contain_band: Any | None
    #: What stands in front of the surface, grown by :data:`FOREGROUND_GROW_PX` and the key's feather; ``None``: none.
    foreground: Any | None
    #: Its share of the quad, as matted (``None``: no matte read).
    coverage: float | None
    #: The ring past the quad :data:`RESIDUAL_MARGIN` of the frame's width wide (``None``: not keyed).
    margin: Any | None
    #: The source's own pixels of the key's hue as drawn, ungrown: the one place a seam is the source's.
    source_own: Any | None = None

    def clean(self, inside: Any, source_alpha: Any) -> Any:
        """The quad's pixels the colour and sharpness gates judge: the source drawn opaque there, clear of what stands in
        front and of the contain bands."""
        judged = inside & (source_alpha >= 0.5)
        if self.foreground is not None:
            judged &= ~self.foreground
        if self.contain_band is not None:
            judged &= ~self.contain_band
        return judged

    def residual_zone(self, inside: Any) -> Any:
        """Where the residual key is read: the quad and its margin, less the source's own key hue, the filled contain
        bands and what stands in front."""
        zone = inside | self.margin if self.margin is not None else inside.copy()
        zone &= ~self.source_keyhue
        if self.contain_band is not None:
            zone &= ~self.contain_band
        if self.foreground is not None:
            zone &= ~self.foreground
        return zone


def gate_masks(
    shown: Any,
    source_alpha: Any,
    key: Any,
    inside: Any,
    *,
    contain_band: Any | None,
    foreground: Any | None,
    margin: Any | None,
    scale: float,
    feather: float,
) -> GateMasks:
    """The :class:`GateMasks` of one frame: *shown* is the source warped into the drawn quad (``H x W x 3``), its alpha
    *source_alpha* (0-1), *inside* the quad, *foreground* what the matte says stands in front (raw), *feather* the
    matte's soft edge in the box's pixels and *scale* the frame's height over 1080."""
    import numpy as np
    from scipy import ndimage

    nothing = np.zeros(inside.shape, dtype=bool)
    opaque = np.asarray(source_alpha) >= 0.5
    source_keyhue = source_complement = own = nothing
    if key is not None:
        hsv = _hsv(shown)
        grow = max(1, math.ceil(SOURCE_HUE_GROW_PX * scale))
        own = _hue_outliers(hsv, key, complement=False, saturation=RESIDUAL_MIN_SATURATION) & opaque
        opposite = _hue_outliers(hsv, key, complement=True) & opaque
        source_keyhue = ndimage.binary_dilation(own, iterations=grow) if own.any() else nothing
        source_complement = ndimage.binary_dilation(opposite, iterations=grow) if opposite.any() else nothing
    grown = None
    coverage = None
    if foreground is not None:
        front = np.asarray(foreground, dtype=bool)
        quad_pixels = int(inside.sum())
        coverage = round(float((front & inside).sum()) / quad_pixels, 4) if quad_pixels else 0.0
        grown = grown_front(front, scale, feather) if front.any() else None
    return GateMasks(source_keyhue, source_complement, contain_band, grown, coverage, margin, own)


def key_seams(keyed: Any, inside: Any, scale: float) -> Any:
    """The pixels of *keyed* - the composed frame's key hue where the source drawn there is not of it - in straight thin
    runs across the quad *inside*: columns down it or rows across it, :data:`SEAM_MAX_PX` (of a 1080p frame,
    *scale* the frame's height over 1080) wide at most, each of the key's hue on at least :data:`SEAM_RUN_SHARE` of the
    quad's extent there. A seam of the compositing - a column the matte or a mask left - never a picture's content."""
    import numpy as np
    from scipy import ndimage

    found = np.zeros(np.shape(inside), dtype=bool)
    hued = np.asarray(keyed, dtype=bool) & inside
    widest = max(1, math.ceil(SEAM_MAX_PX * scale))
    for axis in (0, 1):
        extent = inside.sum(axis=axis)
        lines = (extent >= SEAM_MIN_EXTENT_PX) & (hued.sum(axis=axis) >= SEAM_RUN_SHARE * extent)
        labels, count = ndimage.label(lines)
        for index in range(1, count + 1):
            group = labels == index
            if int(group.sum()) > widest:
                continue
            if axis == 0:
                found[:, group] |= hued[:, group]
            else:
                found[group, :] |= hued[group, :]
    return found


def key_edge_lines(keyed: Any, quad_px: Any, scale: float) -> dict[str, int]:
    """The lines of *keyed* (the composed frame's key hue where the residual key is read, boolean over the box) along
    the edges of *quad_px* (the box's pixels): ``{side: width}``, the width the count of one-pixel offsets of the band
    :data:`EDGE_LINE_BAND_PX` either side of the edge (*scale*: the frame's height over 1080) at which the key's hue runs
    along at least :data:`EDGE_LINE_RUN_SHARE` of the edge. A thin line of the key along a side, inside the quad or
    just past it, that the residual key's share of the quad never reaches."""
    import numpy as np

    mask = np.asarray(keyed, dtype=bool)
    height, width = mask.shape
    reach = max(1, math.ceil(EDGE_LINE_BAND_PX * scale))
    corners = np.asarray(quad_px, dtype=float)
    out: dict[str, int] = {}
    for index, (start, end) in enumerate(zip(corners, np.roll(corners, -1, axis=0), strict=True)):
        along = end - start
        length = float(np.hypot(along[0], along[1]))
        if length < SEAM_MIN_EXTENT_PX:
            continue
        normal = np.array([-along[1], along[0]]) / length
        points = start + ((np.arange(int(length)) + 0.5) / length)[:, None] * along
        lines = 0
        for offset in range(-reach, reach):
            at = points + (offset + 0.5) * normal
            xs, ys = np.floor(at[:, 0]).astype(int), np.floor(at[:, 1]).astype(int)
            within = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
            hit = np.zeros(len(at), dtype=bool)
            hit[within] = mask[ys[within], xs[within]]
            if hit.mean() >= EDGE_LINE_RUN_SHARE:
                lines += 1
        if lines:
            out[SIDES[index]] = lines
    return out


def grown_front(front: Any, scale: float, feather: float) -> Any:
    """*front* (what stands in front, boolean) grown past its edge ring: :data:`FOREGROUND_GROW_PX` pixels of a
    1080p frame (*scale*: the frame's height over 1080) and the key's *feather* (pixels) - what every gate leaves out."""
    from scipy import ndimage

    return ndimage.binary_dilation(front, iterations=max(1, math.ceil(FOREGROUND_GROW_PX * scale + feather)))


def edge_front(matte: Any, quad_px: Any, band: float, scale: float, feather: float) -> Any | None:
    """What stands in front of a keyed surface's edges on one frame: what the bezel lock and the flatness leave
    out of their readings, so a subject crossing an edge never gives its silhouette as the bezel. ``None``: nothing.

    The foreground as every gate reads it (:func:`_foreground`: the non-key regions of *matte* - the key's share,
    ``H x W`` - that reach the surface's rim from outside or hold :data:`FOREGROUND_MIN_SHARE` of it), read within the
    surface's own outline (:func:`surface_outline`) and the lock's search *band* around the seed *quad_px* (pixels of
    *matte*): what stands in front of a surface stands inside its outline, while the bezel inside a seed off it, the
    wall between a straight seed and a curved bezel, a key colour past the bezel all lie outside it. Grown as
    :class:`GateMasks` grows the foreground (:func:`grown_front`, the key's *feather* in pixels, *scale* the frame's
    height over 1080). The same on a save's frame, a preview's and a render's.
    """
    import numpy as np

    values = np.asarray(matte)
    seed = np.asarray(quad_px, dtype=float).reshape(4, 2)
    box = (0, 0, values.shape[1], values.shape[0])
    reach = _coverage(_grown_px(seed, float(band)), box, oversample=1) > 0.5
    outline = surface_outline(values >= 0.5, reach)
    if outline is None:
        return None
    front = _foreground(values < 0.5, outline & reach, scale)
    return grown_front(front, scale, feather) if front.any() else None


def surface_outline(key: Any, within: Any) -> Any | None:
    """The outline of the keyed surface: the convex hull of the key regions of *key* (boolean) within *within*
    holding at least :data:`SURFACE_PART_SHARE` of the largest, as a mask; ``None`` with no key there. A surface is
    flat and convex - a subject in front of it carves into it, never out of its hull."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage
    from scipy.spatial import ConvexHull, QhullError

    labels, count = ndimage.label(np.asarray(key, dtype=bool) & np.asarray(within, dtype=bool))
    if count == 0:
        return None
    sizes = np.bincount(labels.ravel())[1:]
    parts = np.isin(labels, np.flatnonzero(sizes >= SURFACE_PART_SHARE * float(sizes.max())) + 1)
    rim = parts & ~ndimage.binary_erosion(parts)
    ys, xs = np.nonzero(rim)
    if len(xs) < 3:
        return None
    points = np.column_stack([xs, ys]).astype(float)
    try:
        hull = ConvexHull(points)
    except QhullError:
        return None
    drawn = Image.new("L", (labels.shape[1], labels.shape[0]), 0)
    ImageDraw.Draw(drawn).polygon([tuple(points[index]) for index in hull.vertices], fill=255, outline=255)
    return np.asarray(drawn) > 0


def _side_alignment(found_px: Any, quad_px: Any, width: float, edges: Any) -> dict[str, float] | None:
    """``{side: share of the frame's width}``: how far the bezel found (*found_px*) stands off the given quad
    (*quad_px*) on each side whose edge was found (*edges*), the mean of its two corners."""
    import numpy as np

    shifts = np.linalg.norm(np.asarray(found_px, dtype=float) - np.asarray(quad_px, dtype=float), axis=1)
    out = {
        side: round(float(shifts[index] + shifts[(index + 1) % len(SIDES)]) / 2.0 / max(float(width), 1.0), 5)
        for index, side in enumerate(SIDES)
        if edges[index]
    }
    return out or None


def contained_quad(picture_size: tuple[int, int], quad_px: Any) -> Any | None:
    """Where a contain fit draws a picture of *picture_size* inside *quad_px* (pixels): its rectangle's corners
    through the quad's perspective, as ``pictures.fitted`` centres it on the quad's proportions; ``None`` when the two
    share their proportions and no band is left."""
    import numpy as np

    width, height = (float(v) for v in picture_size)
    quad_w, quad_h = quad_size(tuple(map(tuple, quad_px)))
    if width <= 0 or height <= 0 or quad_w <= 0 or quad_h <= 0:
        return None
    target = quad_w / quad_h
    if abs(width / height - target) < 1e-3:
        return None
    canvas_w, canvas_h = (width, width / target) if width / height > target else (height * target, height)
    left, top = (canvas_w - width) / 2.0, (canvas_h - height) / 2.0
    a, b, c, d, e, f, g, h = _solve(((0, 0), (canvas_w, 0), (canvas_w, canvas_h), (0, canvas_h)), [tuple(p) for p in quad_px])
    corners = []
    for x, y in ((left, top), (left + width, top), (left + width, top + height), (left, top + height)):
        w = g * x + h * y + 1.0
        corners.append(((a * x + b * y + c) / w, (d * x + e * y + f) / w))
    return np.asarray(corners, dtype=float)


def _keyish(colour: Any, key: Any) -> bool:
    """Whether *colour* (RGB) is of the key's hue or its complement, as the halo gate reads one (:func:`_hue_outliers`)."""
    import numpy as np

    hsv = _hsv(np.asarray(colour, dtype=np.float32).reshape(1, 1, 3))
    return bool((_hue_outliers(hsv, key, complement=False) | _hue_outliers(hsv, key, complement=True))[0, 0])


def _edges_of(picture: Any, across: bool) -> tuple[str, tuple[float, float, float] | None]:
    """The two edges of *picture* a contain fit's bands lie against - its top and bottom rows when the bands are
    above and below it (*across*), else its left and right columns: ``("transparent", None)`` when they are mostly
    transparent (a mark on transparency), ``("flat", colour)`` when opaque and one colour, the same on both, else
    ``("varied", None)``."""
    import numpy as np

    width, height = picture.size
    depth = max(1, min(4, (height if across else width) // 100))
    boxes = (
        ((0, 0, width, depth), (0, height - depth, width, height))
        if across
        else ((0, 0, depth, height), (width - depth, 0, width, height))
    )
    colours = []
    for box in boxes:
        pixels = np.asarray(picture.crop(box).convert("RGBA"), dtype=np.float32).reshape(-1, 4)
        opaque = pixels[:, 3] >= OPAQUE_ALPHA * 255.0
        if opaque.mean() < 0.5:
            return "transparent", None
        if opaque.mean() < 0.9 or float(pixels[opaque, :3].std(axis=0).max()) >= EDGE_FLAT_SPREAD:
            return "varied", None
        colours.append(pixels[opaque, :3].mean(axis=0))
    if float(np.abs(colours[0] - colours[1]).max()) >= EDGE_FLAT_SPREAD:
        return "varied", None
    mean = (colours[0] + colours[1]) / 2.0
    return "flat", (float(mean[0]), float(mean[1]), float(mean[2]))


def band_backdrop(picture: Any, quad_px: Any, key: Any, bezel: Any) -> tuple[str, tuple[float, float, float] | None]:
    """What fills the bands a contain fit leaves inside a keyed insert's quad: ``(fill, colour)``.

    The source's own edge colour along them when that edge is opaque and one
    flat colour (the bands then read as the source running to the bezel);
    else the bezel's colour when it is dark (*bezel*, a callable reading it,
    :func:`bezel_colour`: the screen's black as the footage shows it); else
    black. A colour of the key's hue is never one (:func:`_keyish`). A source
    whose edge is transparent - a mark on transparency - keeps the keyed
    surface around it (``("surface", None)``): the board it sits on, never a
    card. *quad_px* is the quad drawn into.
    """
    width, height = picture.size
    quad_w, quad_h = quad_size(tuple(map(tuple, quad_px)))
    state, colour = _edges_of(picture, width / max(height, 1) > quad_w / max(quad_h, 1e-6))
    if state == "transparent":
        return "surface", None
    if state == "flat" and colour is not None and not _keyish(colour, key):
        return "source edge", colour
    return screen_off(key, bezel)


def screen_off(key: Any, bezel: Any) -> tuple[str, tuple[float, float, float]]:
    """``(fill, colour)`` a keyed surface shows where nothing is drawn on it: the bezel's colour when it
    is dark and not of the key's hue (*bezel*, or a callable reading it, :func:`bezel_colour`: the screen's black as the
    footage shows it), else black - a contain fit's bands, and the surface before its insert comes in and after it
    goes (:func:`compose`'s *visibility*)."""
    import numpy as np

    dark = bezel() if callable(bezel) else bezel
    if dark is not None and float(np.asarray(dark, dtype=np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)) <= MAX_BACKDROP_LUMA and not _keyish(dark, key):
        return "bezel", tuple(float(v) for v in dark)
    return "black", (0.0, 0.0, 0.0)


def band_key_colour(region: Any, banded: Any) -> tuple[str, float] | None:
    """The chroma key colour the footage *region* (``H x W x 3``) shows in the contain bands *banded* (a mask of its
    shape): ``(name, share)`` - the colour of :data:`screens.KEY_CANDIDATES` whose channel leads past
    :data:`BAND_KEY_LEAD` on the most band pixels, and their share - when that share reaches
    :data:`BAND_KEY_MIN_SHARE`; else ``None``."""
    import numpy as np

    zone = np.asarray(banded, dtype=bool)
    total = int(zone.sum())
    if not total:
        return None
    pixels = np.asarray(region, dtype=np.float32)[zone][:, :3]
    red, green, blue = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    leads = {
        "green": green - np.maximum(red, blue),
        "blue": blue - np.maximum(red, green),
        "magenta": np.minimum(red, blue) - green,
    }
    name, lead = max(leads.items(), key=lambda item: int((item[1] > BAND_KEY_LEAD).sum()))
    share = float((lead > BAND_KEY_LEAD).sum()) / total
    return (name, round(share, 4)) if share >= BAND_KEY_MIN_SHARE else None


def _backdrop(flat: Any, banded: Any, picture: Any, quad_px: Any, key: Any, bezel: Any, light: Any, strength: float) -> tuple[Any, str | None, tuple | None]:
    """What a keyed layer draws under its source: ``(pixels, fill, colour)`` - *flat*, the keyed surface, unless
    a contain fit leaves bands (*banded*), then :func:`band_backdrop`'s colour with the footage's *light* laid over it
    at *strength* as over the source (:func:`lay_light`); ``fill`` ``None`` without bands."""
    import numpy as np

    if banded is None:
        return flat, None, None
    fill, colour = band_backdrop(picture, quad_px, key, bezel)
    if colour is None:
        return flat, fill, None
    under = np.empty(np.asarray(flat).shape, dtype=np.float32)
    under[...] = np.asarray(colour, dtype=np.float32)
    if light is not None and strength > 0:
        under = lay_light(under, light, strength)
    return under, fill, colour


def _partly_shown(rgb: Any, front: Any, surface_share: Any, key: Any, bezel: Any, light: Any, strength: float, visibility: float) -> Any:
    """A keyed layer's pixels *rgb* (its insert all shown) with *visibility* of the insert shown: on the surface
    (*surface_share*) the rest is the colour it shows with nothing on it (:func:`screen_off`, *bezel* its colour or a
    callable reading it), the footage's *light* laid over it at *strength* as over the source; what stands in front
    (*front*, its colours) as drawn."""
    import numpy as np

    _fill, colour = screen_off(key, bezel)
    off = np.empty(np.asarray(rgb).shape, dtype=np.float32)
    off[...] = np.asarray(colour, dtype=np.float32)
    if light is not None and strength > 0:
        off = lay_light(off, light, strength)
    blank = front * (1.0 - surface_share)[..., None] + off * surface_share[..., None]
    return np.clip(visibility * rgb + (1.0 - visibility) * blank, 0.0, 255.0)


@dataclass(frozen=True)
class KeyRead:
    """Where a composited frame shows *key*'s colour around a quad (:func:`key_read`), each a share of the quad."""

    #: All of it, inside the quad and within :data:`RESIDUAL_MARGIN` of the frame's width of it (:func:`key_left`) ...
    share: float = 0.0
    #: ... of it, inside the quad past the strip :data:`RESIDUAL_MARGIN` wide along its edge: the insert's own surface ...
    inside: float = 0.0
    #: ... inside the quad on that strip along its edge: the bezel's glow a quad snapped within a pixel or two meets,
    #: the encoder's chroma bleeding across the bezel - never the insert gone ...
    edge: float = 0.0
    #: ... and past the quad, within the margin: the footage's own key colour around the bezel.
    margin: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {"inside": round(self.inside, 5), "edge": round(self.edge, 5), "margin": round(self.margin, 5)}


def key_read(frame: Any, quad_px: Any, key: Any) -> KeyRead:
    """Where a composited *frame* (``H x W x 3``, the quad's corners *quad_px* in its pixels) still shows *key*'s
    colour, in connected regions as the residual key reads it (:func:`_residual_key`): inside the quad, on the strip
    along its edge, past it within :data:`RESIDUAL_MARGIN`. What the render's guard and the presence gate read
    on the encoded frames (``insert_checks.dropped``, ``insert_checks.PresenceGate``); nothing for a quad off
    the frame."""
    import numpy as np
    from scipy import ndimage

    height, width = frame.shape[:2]
    corners = np.asarray(quad_px, dtype=float)
    reach = max(1.0, RESIDUAL_MARGIN * width)
    x0, y0 = max(0, math.floor(corners[:, 0].min() - reach - 1)), max(0, math.floor(corners[:, 1].min() - reach - 1))
    x1, y1 = min(width, math.ceil(corners[:, 0].max() + reach + 1)), min(height, math.ceil(corners[:, 1].max() + reach + 1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return KeyRead()
    inside = _coverage(corners, (x0, y0, x1, y1)) > 0.5
    quad_pixels = int(inside.sum())
    if not quad_pixels:
        return KeyRead()
    near = ndimage.distance_transform_edt(~inside) <= reach
    share, left = _residual_key(_hsv(np.asarray(frame[y0:y1, x0:x1], dtype=np.float32)), key, near, quad_pixels)
    if left is None:
        return KeyRead(share=share)
    # The quad's edge where the frame's border is not: a quad running off the frame has no bezel on that side.
    padded = np.pad(inside, 1, mode="edge")
    core = (ndimage.distance_transform_edt(padded)[1:-1, 1:-1] > reach) & inside
    count = max(quad_pixels, 1)
    return KeyRead(
        share=share,
        inside=round(float((left & core).sum()) / count, 5),
        edge=round(float((left & inside & ~core).sum()) / count, 5),
        margin=round(float((left & ~inside).sum()) / count, 5),
    )


def key_left(frame: Any, quad_px: Any, key: Any) -> float:
    """The share of a quad still showing *key*'s colour on a composited *frame* (``H x W x 3``, the quad's corners
    *quad_px* in its pixels) - inside it and within :data:`RESIDUAL_MARGIN` of the frame's width of it, in connected
    regions, as the residual key reads it (:func:`_residual_key`, :func:`key_read`); 0 for a quad off the frame."""
    return key_read(frame, quad_px, key).share


@dataclass(frozen=True)
class Subject:
    """What stands in front of a keyed surface over its clip, by colour (:func:`subject_of`): read once and held
    on every frame drawn, so a colour it holds is its own wherever a frame cuts it - a muzzle's tip alone in the quad,
    blurred by its motion."""

    #: The share of its pixels of each hue, the wheel in :data:`OWN_HUE_BIN_DEGREES` bins from 0 degrees: a pixel holds
    #: a hue saturated past :data:`HALO_MIN_SATURATION` and not dark; a grey, a white or a black count in the whole.
    hues: tuple[float, ...]
    #: How many of its pixels were read, on how many frames.
    pixels: int
    frames: int

    def _held(self) -> Any:
        """Each bin's share with the two beside it: a hue's share, whichever bin it falls in."""
        import numpy as np

        shares = np.asarray(self.hues, dtype=np.float32)
        return shares + np.roll(shares, 1) + np.roll(shares, -1)

    def owns(self, degrees: Any) -> Any:
        """Which of *degrees* (hues, an array) are its own: held by :data:`OWN_HUE_MIN_SHARE` of its pixels at least."""
        import numpy as np

        held = self._held()
        bins = (np.asarray(degrees, dtype=np.float32) % 360.0 / OWN_HUE_BIN_DEGREES).astype(int) % len(held)
        return held[bins] >= OWN_HUE_MIN_SHARE

    def near(self, degrees: float, apart: float | None = None) -> tuple[float, float | None]:
        """The share of its pixels within *apart* degrees of *degrees* (the halo's own tolerance by default), and their
        mean hue (``None``: none)."""
        import numpy as np

        apart = HALO_HUE_DEGREES if apart is None else apart
        shares = np.asarray(self.hues, dtype=np.float64)
        centres = (np.arange(len(shares)) + 0.5) * OWN_HUE_BIN_DEGREES
        within = np.abs((centres - float(degrees) + 180.0) % 360.0 - 180.0) <= apart
        share = float(shares[within].sum())
        if share <= 0.0:
            return 0.0, None
        angle = np.radians(centres[within])
        mean = math.degrees(math.atan2(float((shares[within] * np.sin(angle)).sum()), float((shares[within] * np.cos(angle)).sum())))
        return round(share, 4), round(mean % 360.0, 1)

    def as_dict(self) -> dict:
        return {"hues": list(self.hues), "pixels": self.pixels, "frames": self.frames}

    @classmethod
    def from_dict(cls, data: Any) -> Subject | None:
        if not isinstance(data, dict) or not data.get("hues"):
            return None
        return cls(tuple(float(share) for share in data["hues"]), int(data.get("pixels") or 0), int(data.get("frames") or 0))


def subject_of(readings: Any, key: Any, tolerance: float | None, *, feather: float, still: bool) -> Subject | None:
    """The :class:`Subject` in front of a keyed surface over *readings* - its clip's frames, each ``(pixels, quad,
    within)``: ``H x W x 3`` uint8, its quad and the box it is drawn on (0-1 corners; ``None``: the quad grown, as
    :func:`compose` grows it) -, keyed on *key* (``screens.Key``) at *tolerance*, *feather* its soft edge in pixels of a
    1080p frame.

    What stands in front on a frame is what every gate reads (:func:`_foreground`: the non-key regions entering the
    quad) and, on a *still* shot, what of it goes on past the quad: what differs by :data:`CHANGE_MIN` from the clip's
    own background - the median of its frames - and is joined to it, so a muzzle whose tip alone crosses the quad is
    read whole. Its edge, as far in as the edge despill reaches (:data:`EDGE_DESPILL_PX` and twice the feather), is
    left out: the surface's spill there is never its colour. ``None`` with fewer than :data:`SUBJECT_MIN_PIXELS` of it.
    """
    import numpy as np
    from scipy import ndimage

    from app.services.video_studio import screens

    read = []
    for pixels, quad, within in readings:
        height, width = pixels.shape[:2]
        box = bbox(list(within) if within is not None else grown_box(quad, width / max(height, 1)), width, height)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        region = np.ascontiguousarray(np.asarray(pixels)[y0:y1, x0:x1, :3], dtype=np.uint8)
        inside = _coverage(np.array([(x * width, y * height) for x, y in quad], dtype=float), box) > 0.5
        scale = height / 1080.0
        nonkey = screens.key_matte(region.astype(np.float32), key, tolerance) < 0.5
        read.append((region, nonkey, inside, _foreground(nonkey, inside, scale), scale))
    if not read:
        return None
    background = None
    if still and len(read) >= 3 and len({item[0].shape for item in read}) == 1:
        background = np.median(np.stack([item[0] for item in read]), axis=0).astype(np.float32)
    bins = round(360.0 / OWN_HUE_BIN_DEGREES)
    counts = np.zeros(bins, dtype=np.float64)
    total = frames = 0
    for region, nonkey, inside, front, scale in read:
        if not front.any():
            continue
        whole = front
        if background is not None:
            moving = (np.abs(region.astype(np.float32) - background).max(axis=-1) > CHANGE_MIN) & nonkey & ~inside
            labels, _count = ndimage.label(front | moving)
            joined = np.unique(labels[front])
            whole = np.isin(labels, joined[joined > 0])
        reach = math.ceil(max(2.0, EDGE_DESPILL_PX * scale + 2.0 * feather * scale))
        interior = ndimage.binary_erosion(whole, iterations=reach)
        if not interior.any():
            continue
        hsv = _hsv(region[interior].reshape(-1, 1, 3).astype(np.float32)).reshape(-1, 3)
        hued = (hsv[:, 1] >= HALO_MIN_SATURATION) & (hsv[:, 2] >= 0.15)
        counts += np.bincount((hsv[hued, 0] * 360.0 / OWN_HUE_BIN_DEGREES).astype(int) % bins, minlength=bins)
        total += int(interior.sum())
        frames += 1
    if total < SUBJECT_MIN_PIXELS:
        return None
    return Subject(tuple(round(float(count) / total, 5) for count in counts), total, frames)


@dataclass(frozen=True)
class Edge:
    """The edge of what stands in front of a keyed surface on one frame: where the edge despill acts, and
    which of the key's hue or its complement on it is the subject's own colour (:meth:`owned`) - never spill."""

    #: Within :attr:`reach` pixels of the foreground's boundary, either side (boolean over the box).
    zone: Any
    #: The footage's key hue running on at its saturation into the subject (boolean over the box).
    own_key: Any
    reach: float
    #: The pixels the compositor's clamp touched on this frame (:data:`CLAMP_MOVED`; boolean over the box).
    clamped: Any = None
    #: The subject's own hues over its clip (:class:`Subject`; ``None``: not read), and the author's (hue spans,
    #: :func:`own_hue_spans`).
    subject: Subject | None = None
    declared: tuple[tuple[float, float], ...] = ()
    #: Where a colour is held to the subject's interior (:func:`_runs_on`): the window of the box around the zone, the
    #: interior's pixels there, and the Gaussian's sigma.
    window: tuple[slice, slice] | None = None
    core: Any = None
    sigma: float = 1.0

    def hues_owned(self, degrees: Any) -> Any:
        """Which of *degrees* (hues, an array) the subject holds over its clip or the author declares."""
        owned = in_spans(degrees, self.declared)
        return owned | self.subject.owns(degrees) if self.subject is not None else owned

    def owned(self, hsv: Any, keyed: Any, opposite: Any, key: Any) -> Any:
        """Which of the outliers on *hsv* (:func:`_hsv` of the colours read, over the box) - *keyed*, of *key*'s hue,
        and *opposite*, of its complement - are the subject's own: the key's hue it holds over its clip, declares or
        runs on into it in the footage; the complement anywhere the clamp did not touch, where it runs on into the
        subject in these colours (what the clamp left is its own colour), or that it declares. A complement the clamp
        made is never the subject's for being a colour it holds: an inverting despill's magenta on a magenta jacket is
        still the clamp's. Read on the edge's zone: past it, nothing is despilled nor counted."""
        import numpy as np

        degrees = hsv[..., 0] * 360.0
        declared = in_spans(degrees, self.declared)
        complement = ~self.clamped if self.clamped is not None else np.ones(keyed.shape, dtype=bool)
        if self.window is not None and self.clamped is not None:
            # Held to the interior only where the clamp touched the complement on the edge: mostly nowhere.
            made = (opposite & self.clamped & self.zone)[self.window]
            part = _around(made, math.ceil(3.0 * self.sigma))
            if part is not None:
                inner = _within(self.window, part)
                complement = complement.copy()
                complement[inner] |= _runs_on(hsv[inner], self.core[part], key, True, self.sigma)
        return (keyed & (self.own_key | self.hues_owned(degrees))) | (opposite & (complement | declared))


def edge_of(
    region: Any,
    foreground: Any,
    key: Any,
    reach: float,
    *,
    drawn: Any = None,
    subject: Subject | None = None,
    declared: Any = (),
    extent: float = 0.0,
) -> Edge | None:
    """The :class:`Edge` of *foreground* (boolean over the box) on *region* (the raw footage there, ``H x W x 3``) and
    *drawn* (the same pixels as the compositor's clamp leaves them; ``None``: as the footage shows them); *extent* how
    far past the boundary anything is read in pixels, *reach* at least (the edge limit's own, :meth:`Despill.reach`).

    Spill is an edge gradient, never a colour: the surface's light on a
    subject's edge is most saturated there and falls off within a few pixels,
    a colour of the subject's own runs on into it. So the key's hue the raw
    footage shows on the subject or its edge is its own where at least
    :data:`RUN_ON_SHARE` of its interior around it - past twice *reach* from
    its boundary, or a thinner part's own ridge - holds that hue no less
    saturated by :data:`SPILL_FALLOFF` (:func:`_runs_on`). The complement is never a
    bleed of the key: the footage's own colour wherever the compositor's
    clamp did not touch the pixel (*drawn* off *region* by
    :data:`CLAMP_MOVED`), the clamp's own magenta - an inverting despill -
    where it did and the hue does not run on. The subject's hues over its
    clip (*subject*) and the author's (*declared*, :func:`own_hue_spans`)
    are its own on every frame (:meth:`Edge.owned`). ``None`` without a
    foreground, or with one over the whole box.
    """
    import numpy as np
    from scipy import ndimage

    if foreground is None:
        return None
    front = np.asarray(foreground, dtype=bool)
    if not front.any() or front.all():
        return None
    depth = ndimage.distance_transform_edt(front)
    zone = (depth <= reach) & (ndimage.distance_transform_edt(~front) <= reach)
    raw = np.asarray(region, dtype=np.float32)
    clamped = np.zeros(front.shape, dtype=bool)
    if drawn is not None:
        clamped = np.abs(np.asarray(drawn, dtype=np.float32) - raw).max(axis=-1) >= CLAMP_MOVED
    own_key = np.zeros(front.shape, dtype=bool)
    spans = tuple(declared or ())
    if not zone.any():
        return Edge(zone, own_key, float(reach), clamped, subject, spans)
    # Read around the edge alone: the window holds the gradient's reach (three sigmas) past what is read.
    sigma = max(1.0, 2.0 * float(reach))
    window = _around(zone, math.ceil(3.0 * sigma + max(reach, extent)) + 1)
    inner = depth[window]
    # The interior past twice the reach, where no spill gets; a part thinner than that is its own ridge.
    ridge = ndimage.maximum_filter(inner, size=2 * math.ceil(2.0 * reach) + 1)
    core = front[window] & ((inner > 2.0 * reach) | ((inner >= ridge - 0.5) & (inner >= 1.0)))
    # The key's hue the footage shows on what stands in front and its edge (the surface itself is the key's colour,
    # never the subject's own), held to the interior around it alone.
    seen = _hsv(raw[window])
    keyed = _hue_outliers(seen, key, complement=False) & (front[window] | zone[window])
    part = _around(keyed, math.ceil(3.0 * sigma))
    if part is not None:
        own_key[_within(window, part)] = keyed[part] & _runs_on(seen[part], core[part], key, False, sigma)
    return Edge(zone, own_key, float(reach), clamped, subject, spans, window, core, sigma)


def _around(mask: Any, pad: int) -> tuple[slice, slice] | None:
    """The window of *mask*'s shape holding its pixels and *pad* around them; ``None`` with none."""
    import numpy as np

    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    height, width = mask.shape[:2]
    return (
        slice(max(0, int(ys.min()) - pad), min(height, int(ys.max()) + pad + 1)),
        slice(max(0, int(xs.min()) - pad), min(width, int(xs.max()) + pad + 1)),
    )


def _within(window: tuple[slice, slice], part: tuple[slice, slice]) -> tuple[slice, slice]:
    """*part* (a window of an array cut by *window*) as a window of the whole."""
    return tuple(slice(outer.start + inner.start, outer.start + inner.stop) for outer, inner in zip(window, part, strict=True))


def _runs_on(hsv: Any, core: Any, key: Any, complement: bool, sigma: float) -> Any:
    """Which pixels of *hsv* run on into the subject's interior *core* in the key's hue (or its complement):
    at least :data:`RUN_ON_SHARE` of the interior around them - a Gaussian of *sigma* - holds that hue, no less
    saturated there by :data:`SPILL_FALLOFF` than they are."""
    import numpy as np
    from scipy import ndimage

    weight = ndimage.gaussian_filter(core.astype(np.float32), sigma)
    member = (core & _hue_outliers(hsv, key, complement=complement, saturation=HALO_MIN_SATURATION - SPILL_FALLOFF)).astype(np.float32)
    count = ndimage.gaussian_filter(member, sigma)
    level = ndimage.gaussian_filter(member * hsv[..., 1], sigma) / np.maximum(count, 1e-6)
    return (weight > 0.02) & (count >= RUN_ON_SHARE * weight) & (level >= hsv[..., 1] - SPILL_FALLOFF)


def edge_despill(colours: Any, edge: Edge | None, foreground: Any, key: Any, shows: Any = None) -> tuple[Any, float | None]:
    """*colours* (``H x W x 3`` float, the footage's pixels as a keyed layer draws them) with the key's hue taken off the
    edge of what stands in front, and the share of that edge it recoloured (``None``: no edge).

    In the *edge*'s zone - where *shows* (boolean: the footage shows there,
    not the insert) -, a pixel still of the key's hue or its complement
    after the clamp (:func:`_hue_outliers`) is the key's spill on a strand, a
    fringe - unless that colour is the subject's own (:meth:`Edge.owned`):
    it takes the colour and the luminance of its clean neighbours in
    front - a normalised blur of the *foreground*'s pixels of neither hue,
    or of the subject's own -, or its own luminance as a grey where none is
    near.
    """
    import numpy as np
    from scipy import ndimage

    if edge is None:
        return colours, None
    zone = edge.zone & np.asarray(shows, dtype=bool) if shows is not None else edge.zone
    if not zone.any():
        return colours, None
    front = np.asarray(foreground, dtype=bool)
    hsv = _hsv(colours)
    keyed, opposite = _hue_outliers(hsv, key, complement=False), _hue_outliers(hsv, key, complement=True)
    # The subject's own colours are clean neighbours too: a fringe on a pink muzzle takes the muzzle's pink.
    hued = (keyed | opposite) & ~edge.owned(hsv, keyed, opposite, key)
    spill = zone & hued
    if not spill.any():
        return colours, 0.0
    # The clean neighbours are read across the zone's width - a fringe as wide as it finds them - on the window around
    # what it recolours (a blur reaches three sigmas).
    sigma = max(1.0, edge.reach)
    pad = math.ceil(3.0 * sigma)
    ys, xs = np.nonzero(spill)
    y0, y1 = max(0, int(ys.min()) - pad), min(front.shape[0], int(ys.max()) + pad + 1)
    x0, x1 = max(0, int(xs.min()) - pad), min(front.shape[1], int(xs.max()) + pad + 1)
    out = np.array(colours, dtype=np.float32, copy=True)
    window = out[y0:y1, x0:x1]
    clean = (front[y0:y1, x0:x1] & ~hued[y0:y1, x0:x1]).astype(np.float32)
    weight = ndimage.gaussian_filter(clean, sigma)
    neighbours = np.stack([ndimage.gaussian_filter(window[..., c] * clean, sigma) for c in range(3)], axis=-1)
    neighbours /= np.maximum(weight, 1e-6)[..., None]
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    taken = np.where((weight >= 0.05)[..., None], neighbours, np.repeat((window @ weights)[..., None], 3, axis=-1))
    # A blend of clean neighbours that still reads as the key's hue goes grey at its own luminance, unless the subject
    # holds that hue; one of the complement is the subject's own pixels', what the clamp made being none of them.
    again = _hsv(taken)
    still = _hue_outliers(again, key, complement=False) & ~edge.hues_owned(again[..., 0] * 360.0)
    taken = np.where(still[..., None], np.repeat((taken @ weights)[..., None], 3, axis=-1), taken)
    mask = spill[y0:y1, x0:x1]
    window[mask] = taken[mask]
    return out, round(float(spill.sum()) / float(zone.sum()), 4)


def _against_surface(front: Any, inside: Any, reach: float) -> tuple[tuple[slice, slice], Any, Any] | None:
    """Where what stands in front (*front*, boolean over the box) meets the surface inside the quad (*inside*), *reach*
    pixels around: ``(window, depth, apart)`` - the window of the box holding it, and over it each pixel's
    distance to the nearest pixel of the surface (0 on it) and to the nearest pixel in front (0 in front). ``None``
    where nothing in front meets the surface. Read against the surface, never the quad's edge: what crosses the quad
    goes on past it."""
    import numpy as np
    from scipy import ndimage

    surface = inside & ~front
    rim = front & inside & ndimage.binary_dilation(surface, iterations=1)
    if not rim.any():
        return None
    ys, xs = np.nonzero(rim)
    pad = math.ceil(reach) + 2
    height, width = front.shape
    window = (
        slice(max(0, int(ys.min()) - pad), min(height, int(ys.max()) + pad + 1)),
        slice(max(0, int(xs.min()) - pad), min(width, int(xs.max()) + pad + 1)),
    )
    return window, ndimage.distance_transform_edt(~surface[window]), ndimage.distance_transform_edt(~front[window])


def edge_limit(
    colours: Any,
    before: Any,
    foreground: Any,
    inside: Any,
    key: Any,
    despill: Despill,
    reach: float,
    *,
    own: Edge | None = None,
    shows: Any = None,
) -> tuple[Any, float | None]:
    """*colours* (``H x W x 3`` float, the footage as a keyed layer draws it, its spill clamped) with the key's colour
    limited out of the edge of what stands in front, and the share of that edge it corrected (``None``: no
    edge, or ``despill.edge`` off).

    A subject in front of a keyed surface carries the surface's colour on
    its own edge - fur, hair, a sleeve - which the matte keeps in front. The
    clamp (``screens.despill_clamp``) limits a key by its channels: a key a
    model painted between a primary and a secondary (a green leaning cyan)
    is read as two-channelled, its weak channel lifted, and the edge stays
    green. Here, within *reach* pixels of the *foreground*'s boundary against
    the surface (inside the quad, *inside*), every pixel whose hue - on
    *before*, the same pixels before the clamp - lies within
    :data:`HALO_HUE_DEGREES` of the key's (fading out at twice that), or
    whose key channel leads the others (:data:`EDGE_SPILL_LEAD`, in full
    from :data:`EDGE_SPILL_LEAD_FULL`: a skin tone mixed with the key), has the
    key's colour taken out of it (``screens.despill``: its chroma along the
    key's own removed - the key channel limited to the other two for a
    primary key -, its luminance its own). The correction is full on the
    surface's side and on the boundary, fading to nothing *reach* pixels
    into what stands in front: its interior is never touched. It is
    weighted by ``despill.strength``; the subject's own colour there (*own*,
    :func:`edge_of`) is left as it is. The share is of the edge where *shows*
    (the footage shows there, not the insert).
    """
    import colorsys

    import numpy as np

    from app.services.video_studio import screens

    if foreground is None or not despill.edge or despill.strength <= 0.0:
        return colours, None
    front = np.asarray(foreground, dtype=bool)
    within = np.asarray(inside, dtype=bool)
    read = _against_surface(front, within, reach)
    if read is None:
        return colours, None
    window, depth, apart = read
    in_front = front[window]
    band = within[window] & np.where(in_front, depth <= reach + 1.0, apart <= reach)
    weight = np.where(in_front, np.clip((reach + 1.0 - depth) / reach, 0.0, 1.0), 1.0) * band
    pre = np.asarray(before, dtype=np.float32)[window]
    hue, _saturation, _value = _hue_saturation(pre.reshape(-1, 3))
    key_hue = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in key.rgb))[0] * 360.0
    off = np.abs((hue - key_hue + 180.0) % 360.0 - 180.0).reshape(pre.shape[:2])
    hued = np.clip((2.0 * HALO_HUE_DEGREES - off) / HALO_HUE_DEGREES, 0.0, 1.0)
    lead = np.clip((screens.key_share(pre, key) - EDGE_SPILL_LEAD) / (EDGE_SPILL_LEAD_FULL - EDGE_SPILL_LEAD), 0.0, 1.0)
    hued = np.maximum(hued, lead)
    if own is not None:
        hued = np.where(own.own_key[window] | own.hues_owned(hue.reshape(pre.shape[:2])), 0.0, hued)
    share = (weight * hued * float(despill.strength)).astype(np.float32)
    if not (share > 0.0).any():
        return colours, 0.0
    limited = screens.despill(pre, np.zeros(pre.shape[:2], dtype=np.float32), key, share > 0.0)
    out = np.array(colours, dtype=np.float32, copy=True)
    drawn = out[window]
    corrected = drawn + (limited - drawn) * share[..., None]
    moved = np.abs(corrected - drawn).max(axis=-1) >= EDGE_SPILL_MOVED
    out[window] = corrected
    seen = band & np.asarray(shows, dtype=bool)[window] if shows is not None else band
    count = int(seen.sum())
    return out, round(float((moved & seen).sum()) / count, 4) if count else 0.0


def light_wrap(colours: Any, behind: Any, foreground: Any, inside: Any, scale: float, core: Any = None) -> Any:
    """*colours* (the footage as a keyed layer draws it) with the insert's light wrapped onto the edge of what stands in
    front: what a real screen does to a real edge.

    A pixel of the *foreground* inside the quad (*inside*) on its boundary
    against the surface takes :data:`LIGHT_WRAP_SHARE` of the insert's colour
    around it - *behind*, what the surface shows (the source drawn, or what
    is under it), read over the surface clear of the foreground -, half that
    one pixel deeper at 1080p, nothing past :data:`LIGHT_WRAP_PX` pixels of a
    1080p frame (*scale*: the frame's height over 1080) from the surface -
    and never on *core* (:func:`front_core`), where the insert is never drawn:
    on a finger or a muzzle thinner than twice the wrap, the insert's blurred
    strokes wrapped in from both sides would read as the source drawn through
    it (``insert_through_foreground``, #10573).
    """
    import numpy as np
    from scipy import ndimage

    if foreground is None:
        return colours
    front = np.asarray(foreground, dtype=bool)
    within = np.asarray(inside, dtype=bool)
    deep = max(1.0, LIGHT_WRAP_PX * scale)
    # The window holds the blur's reach (three sigmas) around the band.
    read = _against_surface(front, within, 4.0 * deep)
    if read is None:
        return colours
    window, depth, _apart = read
    surface = (within[window] & ~front[window]).astype(np.float32)
    shown = np.asarray(behind, dtype=np.float32)[window]
    weight = ndimage.gaussian_filter(surface, deep)
    light = np.stack([ndimage.gaussian_filter(shown[..., c] * surface, deep) for c in range(3)], axis=-1)
    light /= np.maximum(weight, 1e-6)[..., None]
    edge = front[window] & within[window] & (weight >= 0.05) & (depth <= deep)
    if core is not None:
        edge &= ~np.asarray(core, dtype=bool)[window]
    wrap = LIGHT_WRAP_SHARE * np.clip((deep + 1.0 - depth) / deep, 0.0, 1.0) * edge
    out = np.array(colours, dtype=np.float32, copy=True)
    drawn = out[window]
    out[window] = drawn + (light - drawn) * wrap[..., None]
    return out


def treat_edge(
    colours: Any,
    before: Any,
    foreground: Any,
    inside: Any,
    key: Any,
    despill: Despill,
    edge: Edge | None,
    shows: Any,
    behind: Any,
    *,
    scale: float,
    feather: float,
    matte: Any = None,
    clear: Clear | None = None,
    coverage: Any = None,
    core: Any = None,
) -> tuple[Any, float | None, float | None]:
    """The footage as a keyed layer draws it (*colours*, its spill clamped; *before*, the same pixels before the clamp)
    with the edge of what stands in front treated: the key's colour limited out of it
    (:func:`edge_limit`, ``despill.width_px`` past it and the key's *feather*), what the clamp still leaves of the key's
    hue or its complement given its clean neighbours' colour (:func:`edge_despill`), the key's residue the clamp left
    grey drawn as the insert (:func:`key_residue`: the frame's key *matte*, near the *clear* part of what stands
    in front, inside the quad's *coverage*) and the insert's light (*behind*; ``None`` with no insert drawn, the save's
    reading) wrapped onto it (:func:`light_wrap`, never on the *core* the insert is never drawn on) - with
    ``despill.edge`` off, the residue alone: that edge is then the footage's, the key's clamp alone taken out.
    ``(colours, the share the limit corrected, the share the despill recoloured)``."""
    if not despill.edge:
        return key_residue(colours, behind, matte, clear, coverage, scale), None, None
    colours, limited = edge_limit(colours, before, foreground, inside, key, despill, despill.reach(scale, feather), own=edge, shows=shows)
    colours, recoloured = edge_despill(colours, edge, foreground, key, shows)
    # Before the wrap: the insert's light wrapped onto the residue's grey would lift its chroma out of the residue's reach.
    colours = key_residue(colours, behind, matte, clear, coverage, scale)
    if behind is not None:
        colours = light_wrap(colours, behind, foreground, inside, scale, core)
    return colours, limited, recoloured


@dataclass(frozen=True)
class EdgeSpill:
    """The key's spill on the edge of what stands in front of a keyed surface on one footage frame
    (:func:`edge_spill`): what the save says a preview's halo gate will read, before anything is drawn."""

    #: The share of that edge holding the key's hue or its complement in the footage ...
    share: float
    #: ... what the layer's edge treatment leaves of it at the insert's despill (``None``: ``despill.edge`` off) ...
    after: float | None
    #: ... that edge's length in pixels, and the despill's width (pixels of a 1080p frame).
    edge_px: int
    width_px: float

    def as_dict(self) -> dict:
        return {"share": self.share, "after": self.after, "edge_px": self.edge_px, "width_px": self.width_px}

    @classmethod
    def from_dict(cls, data: dict) -> EdgeSpill:
        after = data.get("after")
        return cls(float(data["share"]), None if after is None else float(after), int(data.get("edge_px") or 0), float(data.get("width_px") or EDGE_SPILL_PX))


def edge_spill(region: Any, matte: Any, inside: Any, key: Any, despill: Despill, *, scale: float, feather: float) -> EdgeSpill | None:
    """The key's spill on the edge of what stands in front of a keyed surface, read on one footage frame before
    anything is drawn: the save's word on what a preview's halo gate will read there.

    What stands in front is the matte's (:func:`_foreground` on *matte*, the key's share, inside the quad *inside*);
    its edge the halo's band on its own side (:func:`_halo_band`) - where the footage shows, the insert drawn past it.
    Read on *region* (the footage there, ``H x W x 3`` float): the share of that edge of the key's hue or its
    complement, the subject's own colours left out (:meth:`Edge.owned`: on the footage as it is, the complement
    is never spill), and the share left once the layer's clamp (``screens.despill_clamp``) and its edge treatment at
    *despill* (:func:`treat_edge`) took it out. *feather* is the key's, in pixels; *scale* the frame's height over 1080.
    ``None`` with nothing in front, or an edge too short.
    """
    from app.services.video_studio import screens

    front = _foreground(matte < 0.5, inside, scale)
    if not front.any():
        return None
    band, boundary, _reach = _halo_band(front, inside, scale)
    ring = band & front
    if int(ring.sum()) < 64:
        return None
    reach = max(2.0, EDGE_DESPILL_PX * scale + 2.0 * feather)

    def held(colours: Any, edge: Edge | None) -> float:
        hsv = _hsv(colours)
        keyed, opposite = _hue_outliers(hsv, key, complement=False), _hue_outliers(hsv, key, complement=True)
        outliers = keyed | opposite
        if edge is not None:
            outliers &= ~edge.owned(hsv, keyed, opposite, key)
        return round(float(outliers[ring].mean()), 4)

    after = None
    if despill.edge:
        clamped = screens.despill_clamp(region, key, inside)
        edge = edge_of(region, front, key, reach, drawn=clamped, declared=despill.spans(), extent=despill.reach(scale, feather))
        treated, _limited, _recoloured = treat_edge(clamped, region, front, inside, key, despill, edge, None, None, scale=scale, feather=feather)
        after = held(treated, edge)
    return EdgeSpill(held(region, edge_of(region, front, key, reach, declared=despill.spans())), after, int(boundary.sum()), despill.width_px)


@dataclass
class Fill:
    """A static keyed insert's quad as the page shows it: read once off its first footage frame, drawn on every frame.

    A keyed insert that is not tracked keys nothing frame by frame - a key
    read against compressed footage drifts, and a matte recomputed on every
    frame blinks at its edge; so does a bezel snapped again on every frame.
    Its quad is locked to the bezel once (:func:`_locked`) and filled: under
    the source (where its alpha leaves the surface showing - a contain fit's
    room, a mark's own transparency), the surface as the first footage frame
    shows it with the key's spill taken out (``screens.despill_clamp``), the
    same pixels on every frame. What passes in front of it still stays in
    front: the occlusion every unkeyed insert has (:class:`Surface`: what
    differs from the first frame and enters through the rim), which reads no
    key, pooled over the frames around like a key's matte, its edge feathered
    by the key's feather. The margin around the quad is read on the same frame
    (:class:`Band`): the footage's key colour past the bezel is keyed there at
    the key's tolerance and filled with the bezel's colour on every frame.
    """

    #: The box the layer covers (the page's: the insert's own box, else :func:`grown_box` of the quad), at the host
    #: size read.
    box: tuple[int, int, int, int]
    #: The quad drawn into and the bezel found, in host pixels, and the given quad's error against it.
    drawn_px: Any
    found_px: Any
    alignment: float | None
    #: ``H x W x 3`` float over the box: the first frame's pixels with the key's spill taken out, the surface flat.
    flat: Any
    #: The margin around the quad as the first frame shows it: the key past the bezel and the surface past the quad.
    band: Band | None = None
    #: The bezel's colour on that frame (:func:`bezel_colour`): what may fill a contain fit's bands.
    bezel: tuple[float, float, float] | None = None
    #: The bezel's colour on each side of the quad (:func:`bezel_sides`): what fills the key's hue in the margin.
    bezel_sides: Any | None = None
    #: The bezel lock's reading of that frame (``surface_refine.Refined``): its edges' shape and the sides it found.
    found: Any | None = None
    #: What stands in front of the surface on that frame, as its key's matte says (:func:`_foreground`; boolean
    #: over the box, ``None`` for nothing), and the share of each pixel the insert keeps there, feathered: the
    #: occlusion holds every frame to that one and never sees it, so it is kept in front here.
    front: Any | None = None
    kept: Any | None = None
    #: How sure that mask is (``FrameAudit.front_confidence``): the share of it the key reads clear.
    confidence: float | None = None


def static_fill(insert: InsertSource, first: Any, size: tuple[int, int]) -> Fill | None:
    """The :class:`Fill` of *insert* (keyed, untracked) on *first* - its first footage frame (``H x W x 3`` at *size*).

    Read on the box :func:`compose` draws it on - the insert's own box (``InsertSource.box``, the page's), else the
    quad grown -, so the two always agree. ``None`` for an insert that is tracked, has no key, or whose quad is off
    the host.
    """
    import numpy as np
    from scipy import ndimage

    from app.services.video_studio import screens, surface_refine

    if insert.key is None or insert.track is not None:
        return None
    width, height = size
    quad, within = insert.quads(insert.first_frame)
    if first is None or first.shape[:2] != (height, width):
        return None
    box = bbox(within or grown_box(quad, width / max(height, 1)), width, height)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    origin = np.array([x0, y0], dtype=float)
    quad_px = np.array([(x * width, y * height) for x, y in quad], dtype=float)
    region = first[y0:y1, x0:x1].astype(np.float32)
    key = insert.key.key()
    tolerance = insert.key.matte_tolerance()
    scale = height / 1080.0
    feather = insert.key.feather * scale
    drawn_px, found_px, alignment, raw, found = _locked(
        region, quad_px, origin, width, key, tolerance, surface_refine.Refiner(), feather=feather, scale=scale
    )
    flat = screens.despill_clamp(region, key, np.ones(region.shape[:2], dtype=bool))
    inside = _coverage(drawn_px, box) > 0.5
    distance = ndimage.distance_transform_edt(~inside)
    margin = max(1.0, KEY_MARGIN * width)
    sides = bezel_sides(region, raw, inside, distance, margin, RESIDUAL_MARGIN * width, drawn_px - origin, key)
    band = band_of(region, raw, inside, distance, margin, drawn_px - origin, feather, sides)
    # What already stands in front on this frame: the occlusion of every later frame is read against this one.
    front = _foreground(raw < 0.5, inside, scale)
    kept = None
    confidence = None
    if front.any():
        kept = 1.0 - front.astype(np.float32)
        if feather > 0.05:
            kept = np.clip(ndimage.gaussian_filter(kept, feather), 0.0, 1.0)
        confidence = key_confidence(raw, front, inside)
    else:
        front = None
    colour = bezel_colour(region, raw, inside, distance, margin, key)
    return Fill(box, drawn_px, found_px, alignment, flat, band, colour, sides, found, front, kept, confidence)


@dataclass
class Lock:
    """A static unkeyed insert's quad locked to its bezel once, on its first footage frame: drawn on every frame.

    The keyed one's lock is its :class:`Fill`'s. Snapped again on every frame, the bezel lock costs a frame its
    search and lets the quad wander a pixel where the temporal prior settles; only a tracked insert's quad moves.
    The bezel is still read on every frame against the lock (:func:`_drift_of`): a surface that moves from
    under a locked quad is measured, never drawn after - the audit says how far, the alignment gate judges it.
    """

    #: The box the layer covers, at the host size read: the same one :func:`compose` draws on.
    box: tuple[int, int, int, int]
    #: The quad drawn into and the bezel found, in host pixels, and the given quad's error against it.
    drawn_px: Any
    found_px: Any
    alignment: float | None
    #: The bezel lock's reading of that frame (``surface_refine.Refined``): its edges' shape and the sides it found.
    found: Any | None = None


def static_lock(insert: InsertSource, first: Any, size: tuple[int, int]) -> Lock | None:
    """The :class:`Lock` of *insert* (unkeyed, untracked) on *first* - its first footage frame (``H x W x 3`` at
    *size*) -, read on the box :func:`compose` draws it on. ``None`` for an insert that is tracked, keyed (its
    :func:`static_fill` locks it), or whose quad is off the host."""
    import numpy as np

    from app.services.video_studio import surface_refine

    if insert.key is not None or insert.track is not None:
        return None
    width, height = size
    quad, within = insert.quads(insert.first_frame)
    if first is None or first.shape[:2] != (height, width):
        return None
    box = bbox(within or grown_box(quad, width / max(height, 1)), width, height)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    quad_px = np.array([(x * width, y * height) for x, y in quad], dtype=float)
    scale = height / 1080.0
    drawn_px, found_px, alignment, _raw, found = _locked(
        first[y0:y1, x0:x1].astype(np.float32),
        quad_px,
        np.array([x0, y0], dtype=float),
        width,
        None,
        None,
        surface_refine.Refiner(),
        feather=OCCLUSION_FEATHER * scale,
        scale=scale,
    )
    return Lock(box, drawn_px, found_px, alignment, found)


@dataclass
class Layer:
    """One frame of an insert drawn: its pixels over the host's box, its glow, what was audited, and how it looks there."""

    warped: Warped
    glow: Any | None
    audit: FrameAudit = field(default_factory=FrameAudit)
    #: The insert drawn over the footage, the foreground it covers marked (for a finding's frame).
    evidence: Any | None = None
    #: The same pixels, unmarked, along the side of the quad where most of a residual key sits: the margin past that
    #: edge and as deep inside it, at 100 % (for a residual-key finding).
    residual_crop: Any | None = None
    #: The same pixels around the worst stretch of a halo on the edge of what stands in front, at 100 % (for a
    #: halo finding: :func:`_halo_crop`).
    halo_crop: Any | None = None
    #: The same pixels around the worst stretch of the insert drawn over what stands in front, at 100 % (for an
    #: ``insert_foreground`` finding: :func:`_front_crop`).
    foreground_crop: Any | None = None
    #: The same pixels around the worst stretch of a grey rim on the edge of what stands in front, at 100 % (for
    #: an ``insert_rim`` finding).
    rim_crop: Any | None = None
    #: The same pixels around the largest stretch of the source's strokes drawn across what stands in front, at 100 %
    #: (for an ``insert_through_foreground`` finding: :func:`through_share`'s tiles).
    through_crop: Any | None = None
    #: The whole footage frame, the layer drawn on it and the key's regions past the quad marked (for a
    #: ``key_spill_outside_quad`` finding: :func:`key_spill`).
    spill_frame: Any | None = None
    #: A keyed matte drawn unrefined: :func:`compose` may draw it again refined on the edge (#10573).
    refinable: bool = False

    def pngs(self) -> tuple[bytes, bytes | None]:
        return png(self.warped.image), png(self.glow) if self.glow is not None else None

    def pictures(self) -> dict[str, Any]:
        """What a failing frame shows, by kind: the frame marked, its residual key's side, its halo, the insert over
        what stands in front, its grey rim and the source seen through what stands in front at 100 %, and the whole
        frame with the key past the quad."""
        return {
            "proof": self.evidence,
            "residual_crop": self.residual_crop,
            "halo_crop": self.halo_crop,
            "foreground_crop": self.foreground_crop,
            "rim_crop": self.rim_crop,
            "through_crop": self.through_crop,
            "spill_frame": self.spill_frame,
        }


def compose(
    insert: InsertSource,
    picture: Any,
    size: tuple[int, int],
    footage_frame: int | None = None,
    *,
    footage: Any = None,
    surface: Surface | None = None,
    fill: Fill | None = None,
    lock: Lock | None = None,
    evidence: bool = False,
    state: Matting | None = None,
    footage_ahead: list[tuple[int, Any]] | None = None,
    window_frame: int | None = None,
    visibility: float = 1.0,
) -> Layer | None:
    """*picture* drawn onto its quad (:func:`_compose`), drawn again with the keyed matte refined on the edge of what
    stands in front (:func:`refined_front`) when the frame fails ``insert_through_foreground`` on that edge alone
    (#10573): ``FrameAudit.matte_refined`` says it was, ``FrameAudit.through_edge`` whether what is left is on that
    edge. A frame that passes is drawn once, as it always was. The state's edge hold is read once per frame."""
    arguments: dict[str, Any] = {
        "footage": footage,
        "surface": surface,
        "fill": fill,
        "lock": lock,
        "evidence": evidence,
        "state": state,
        "footage_ahead": footage_ahead,
        "window_frame": window_frame,
        "visibility": visibility,
    }
    import copy

    # What a frame drawn moves in the state - the edge hold's last mattes, the bezel locks' priors -, put back before
    # the same frame is drawn again: the next frame reads them as if it was drawn once.
    held = (state.last, state.before, copy.deepcopy(state.refiner), copy.deepcopy(state.drift)) if state is not None else None
    layer = _compose(insert, picture, size, footage_frame, **arguments)
    if layer is None or not layer.audit.through_foreground or not layer.audit.through_edge or not layer.refinable:
        return layer
    if state is not None and held is not None:
        state.last, state.before, state.refiner, state.drift = held
    again = _compose(insert, picture, size, footage_frame, refine=True, **arguments)
    return again if again is not None else layer


def _compose(
    insert: InsertSource,
    picture: Any,
    size: tuple[int, int],
    footage_frame: int | None = None,
    *,
    footage: Any = None,
    surface: Surface | None = None,
    fill: Fill | None = None,
    lock: Lock | None = None,
    evidence: bool = False,
    state: Matting | None = None,
    footage_ahead: list[tuple[int, Any]] | None = None,
    window_frame: int | None = None,
    visibility: float = 1.0,
    refine: bool = False,
) -> Layer | None:
    """*picture* (the insert's source frame) drawn onto its quad at *size*, matted by *footage*; ``None`` off the host.

    *footage* is the footage frame the page shows under it (``H x W x 3``
    uint8 at *size*), *surface* the reference of its first frame (an
    unkeyed insert's occlusion): with neither, the insert is drawn whole.
    With footage, the quad is snapped to the bezel the frame shows
    (``surface_refine``) and the insert drawn into the refined quad,
    on the box :func:`grown_box` gives (the page's). A static keyed insert
    draws over its *fill* instead (:func:`static_fill`): its bezel
    locked and its surface read once, nothing keyed frame by frame - given
    its *surface* too, what enters the quad through its rim stays in front
    of the fill. A static unkeyed one given its *lock* (:func:`static_lock`)
    draws into the quad locked on its first frame: only a tracked insert's
    bezel is found again frame by frame. *state* carries the
    bezel lock and the mattes around the frame from one frame to the next
    (a page's, a build's), each footage frame's raw matte read once
    (:meth:`Matting.reading`), *footage_ahead* the other footage frames
    (``(frame, pixels)``) its matte's median window reaches - the frames
    after this one, and those before it the state has not read -, numbered
    as *window_frame* numbers this one (the footage frame unless given: a
    build counts its own frames); without a state each frame stands alone.
    A keyed insert's layer carries the whole surface and its margin, the
    footage there despilled and the surface painted flat under the insert;
    another's, the insert alone. *evidence* draws the frame a finding shows.
    *visibility* is how much of the insert shows: a keyed insert held
    on its surface before it comes in, after it goes and through its fades
    is drawn whole over the surface all the same, *visibility* of the insert
    over the colour the surface shows with nothing on it (:func:`screen_off`)
    - the key never shows through; an unkeyed one is drawn at that opacity.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    from app.services.video_studio import screens, surface_refine
    from app.services.video_studio.pictures import content_box, cover_cuts

    quad, within = insert.quads(footage_frame)
    width, height = size
    matted = footage is not None and footage.shape[:2] == (height, width)
    if matted and within is None:
        within = grown_box(quad, width / max(height, 1))
    box = bbox(within or quad, width, height)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    origin = np.array([x0, y0], dtype=float)
    quad_px = np.array([(x * width, y * height) for x, y in quad], dtype=float)
    scale = height / 1080.0
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    key = insert.key.key() if insert.key is not None else None
    region = footage[y0:y1, x0:x1].astype(np.float32) if matted else None
    filled = fill is not None and key is not None and region is not None and fill.box == box
    # A static unkeyed insert's lock (read once, on its first frame) where it was read on this very box.
    held_lock = lock if key is None and insert.track is None and region is not None and lock is not None and lock.box == box else None
    when = window_frame if window_frame is not None else footage_frame
    # How sure the mask of what stands in front is on this frame (``FrameAudit.front_confidence``).
    confidence = None

    def front_of(pixels: Any, frame: int | None, feather_px: float) -> Front:
        # One footage frame's occlusion, its confidence kept with its reading for when that frame is drawn.
        read = surface.front(pixels, quad_px, box, feather_px, full=key is None)
        if state is not None and frame is not None:
            state.confidence[frame] = read.confidence
        return read

    def occlusion_of(feather_px: float) -> tuple[Any, Any]:
        # What stands in front, read on the plane the reference was read on (the given quad): once a footage frame
        # with a state, unless the quad moves frame by frame - then each frame reads its own.
        nonlocal confidence
        if state is None:
            read = surface.front(footage, quad_px, box, feather_px, full=key is None)
            confidence = read.confidence
            return read.visible, read.foreground
        visible = state.reading(when, lambda: front_of(footage, when, feather_px).visible, fresh=insert.track is not None)
        confidence = state.confidence.get(when) if when is not None else None
        return visible, visible < 0.5

    # 1. The surface, snapped to its bezel: a keyed surface's edge is its matte's, another's its luma's,
    # what stands in front of it (read first, on the plane the reference was read on) giving no edge. A filled
    # insert or a locked one was locked once, on its first frame: the same quad on every frame, never a bezel
    # snapped again.
    raw_matte = None
    occlusion = None
    alignment = None
    locked = None
    drawn_px = quad_px
    # The soft edge of what stands in front: a key's own feather, else the occlusion's.
    feather = (insert.key.feather if insert.key is not None else OCCLUSION_FEATHER) * scale
    # A static insert's bezel on this very frame, read against its lock: what moved from under the quad.
    drift_px = drift = drift_read = None
    if filled:
        drawn_px, found_px, alignment = fill.drawn_px, fill.found_px, fill.alignment
        # The frame's own key share: never what stands in front - the occlusion says that -, only how much of
        # its edge's colour is the key's residue.
        raw_matte = screens.key_matte(region, key, insert.key.matte_tolerance())
        if surface is not None:
            occlusion = occlusion_of(feather)
        elif fill.front is not None:
            occlusion = (np.array(fill.kept, copy=True), np.array(fill.front, copy=True))
    elif held_lock is not None:
        drawn_px, found_px, alignment = held_lock.drawn_px, held_lock.found_px, held_lock.alignment
        if surface is not None:
            occlusion = occlusion_of(OCCLUSION_FEATHER * scale)
    elif region is not None:
        hidden = None
        if key is None and surface is not None:
            # What entered the surface since its first frame is known by its motion, never mistaken for the
            # bezel: its readings give no edge.
            read = surface.front(footage, quad_px, box, OCCLUSION_FEATHER * scale)
            occlusion, confidence = (read.visible, read.foreground), read.confidence
            hidden = read.foreground
        refiner = state.refiner if state is not None else surface_refine.Refiner()
        tolerance = insert.key.matte_tolerance() if insert.key is not None else None
        if key is not None and state is not None:
            # Its box is the same on every frame, tracked or not: a matte read ahead for this frame is its matte.
            raw_matte = state.reading(when, lambda: screens.key_matte(region, key, tolerance))
        # The matte's own edge: a person across it is a minority of the readings, which the line fit leaves out;
        # a non-key region inside a misplaced quad is the bezel itself, never left out of the search.
        # The prior holds an untracked quad's bezel only: a tracker's own correction is a real one.
        drawn_px, found_px, alignment, raw_matte, locked = _locked(
            region,
            quad_px,
            origin,
            width,
            key,
            tolerance,
            refiner,
            hidden,
            prior=insert.track is None,
            steady=insert.track is not None,
            feather=feather,
            scale=scale,
            raw_matte=raw_matte,
        )
    else:
        found_px = quad_px
    if (filled or held_lock is not None) and region is not None:
        # The bezel this frame shows, against the lock: read, never drawn - the alignment gate's every frame.
        if state is not None and state.drift is None:
            state.drift = surface_refine.Refiner()
        drift_px, drift, drift_read = _drift_of(
            region,
            found_px,
            origin,
            width,
            state.drift if state is not None else surface_refine.Refiner(),
            occlusion[1] if occlusion is not None else None,
            key=key,
            tolerance=insert.key.matte_tolerance() if insert.key is not None else None,
            matte=raw_matte,
        )
    drawn_quad = [(float(x) / width, float(y) / height) for x, y in drawn_px]
    refined_quad = [(float(x) / width, float(y) / height) for x, y in found_px]

    # 2. The source warped into it, at the output size: a still source on a quad that does not move, once.
    if state is not None and insert.kind != "video":
        warped = state.warp_of(picture, drawn_quad, size, insert.fit, within or drawn_quad)
    else:
        warped = warp(picture, drawn_quad, size, insert.fit, within=within or drawn_quad)
    if warped is None:
        return None
    drawn = np.asarray(warped.image, dtype=np.float32)
    source_alpha = drawn[..., 3] / 255.0
    shown = drawn[..., :3]
    coverage = _coverage(drawn_px, box)
    inside = coverage > 0.5
    # The bands a contain fit leaves inside the quad, where the source is not drawn.
    contained = contained_quad(picture.size, drawn_px) if insert.fit == "contain" else None
    banded = inside & ~(_coverage(contained, box) > 0.5) if contained is not None else None
    if banded is not None and not banded.any():
        banded = None

    # 3. The matte: what stands in front of the surface stays in front; the rest of the surface is painted flat.
    # Where the mask of what stands in front holds, less its edge ring (:func:`front_core`), the insert is
    # never drawn: the mask every gate reads is the one that mattes.
    foreground = None
    core = None
    # The edge ring the core leaves soft: twice the feather deep where a key's matte gives each of its pixels the key's
    # share; the feather alone where the occlusion's mask is only feathered (below).
    reach = max(1, math.ceil(2.0 * feather))
    soft = None
    matte = None
    visible = None
    overlap = None
    alpha = source_alpha
    if region is not None and key is not None and not filled:
        tolerance = insert.key.matte_tolerance()
        ahead = [
            (later, lambda pixels=pixels: screens.key_matte(pixels[y0:y1, x0:x1].astype(np.float32), key, tolerance))
            for later, pixels in footage_ahead or ()
            if pixels.shape[:2] == (height, width)
        ]
        matte = state.smoothed(when, raw_matte, ahead) if state is not None else raw_matte
        foreground = _foreground(matte < 0.5, inside, scale)
        confidence = key_confidence(matte, foreground, inside)
        # Feathered and cored past the quad's edge where the subject crosses it: the quad's edge is never read as
        # the subject's, so neither it nor its antialiased pixels are drawn over the subject (#11460).
        past = front_past_quad(foreground, matte < 0.5, inside, reach + 1)
        soft = past.astype(np.float32)
        if feather > 0.05:
            soft = np.clip(ndimage.gaussian_filter(soft, feather), 0.0, 1.0)
        # Inside the surface and not in front of it a pixel is all surface (painted flat); at the foreground's edge
        # it is the key's share of it, so a person's blurred edge unmixes rather than cuts - and inside that edge
        # none: a subject whose colours the tolerance reads part key is never seen through the insert.
        core = ndimage.binary_erosion(past, iterations=max(1, int(reach))) & inside
        opacity = soft * (1.0 - matte)
        if refine:
            # The edge as the footage shows it: opaque where the subject is, the insert never drawn through (#10573).
            opacity, core = refined_front(region, opacity, foreground, inside, core, scale)
        kept = in_front(1.0 - opacity, core)
        alpha = source_alpha * kept
        if state is not None:
            overlap = state.hold(when, kept)
    elif region is not None and occlusion is not None:
        # Read on the plane the reference was read on (the given quad): the two grids then line up to the pixel.
        visible, foreground = occlusion
        # A feathered mask holds nothing of the subject's edge past the feather: at twice it, a 1.5 px feather
        # on a 1080p frame left a 3 px band of that edge half-drawn over, the insert seen through the fur.
        reach = max(1, math.ceil(feather))
        if state is not None and surface is not None:
            ahead = [
                (later, lambda pixels=pixels, later=later: front_of(pixels, later, feather).visible)
                for later, pixels in footage_ahead or ()
                if pixels.shape[:2] == (height, width)
            ]
            # The window holds the mask still from frame to frame; this frame's own reading stays in front whatever
            # its window says: a subject's head, narrower than three frames of its motion, is never smoothed
            # away from under the insert.
            visible = np.minimum(state.smoothed(when, visible, ahead), visible)
            foreground = visible < 0.5
        if filled and fill.front is not None and surface is not None:
            # What already stood in front on the frame the fill was read on stays in front: the occlusion is
            # held to that frame and never sees it.
            visible = np.minimum(visible, fill.kept)
            foreground = foreground | fill.front
            confidence = min((value for value in (confidence, fill.confidence) if value is not None), default=None)
        elif filled and fill.front is not None:
            confidence = fill.confidence
        if key is not None and raw_matte is not None:
            # A pocket of the key the occlusion closed into what stands in front: this frame's key reads it as
            # the surface, so the insert is drawn there - never the key's colour left beside the subject.
            visible, foreground = key_out_pockets(visible, foreground, inside, raw_matte, reach)
        # Cored past the quad's edge where the subject crosses it: past the quad nothing is drawn, so the quad's edge
        # is never read as the subject's (#11460).
        past = front_past_quad(foreground, ~inside, inside, reach + 1)
        core = ndimage.binary_erosion(past, iterations=max(1, int(reach))) & inside
        visible = in_front(visible, core)
        if state is not None and surface is not None:
            overlap = state.hold(when, visible)
        alpha = source_alpha * visible

    # 3b. The footage's light over the insert: read on the surface it covers - the key's pixels clear of the
    # foreground, or what the occlusion leaves showing - and laid over the source as light, never as a colour.
    lit = shown
    light = None
    if region is not None and insert.light > 0:
        if matte is not None:
            surface_weight = coverage * matte * (1.0 - soft)
        else:
            surface_weight = coverage * (visible if visible is not None else 1.0)
        light = ambient_light(region @ weights, surface_weight, drawn_px - origin)
        if light is not None:
            lit = lay_light(shown, light, insert.light)
    emission = Image.fromarray(np.clip(np.dstack([shown, alpha * 255.0]), 0, 255).astype(np.uint8), "RGBA")
    bloom = None
    if insert.glow > 0:
        # One tint - the insert's own colour - kept to the bezel, never the blurred source over the host.
        bloom = glow(Warped(box, emission), size, quad=drawn_quad, tint=mean_colour(shown, alpha) or (255, 255, 255))

    # 4. The layer.
    near = None
    band = None
    bezel_by_side = None
    distance = None
    edge = None
    edge_despilled = edge_spilled = None
    halo_crop = None
    band_fill = band_rgb = None
    margin = max(1.0, KEY_MARGIN * width)
    # The edge of what stands in front: where the edge despill acts, the key's feather and its own reach.
    edge_reach = max(2.0, EDGE_DESPILL_PX * scale + 2.0 * feather)
    # The part of what stands in front the key reads clear on this frame: its edge's band is where the key's
    # residue is drawn as the insert and where the grey rim is read.
    clear = clear_part(foreground, inside, raw_matte, scale) if key is not None else None
    rim_crop = None
    if filled:
        # The filled surface where it shows, the frame's own pixels - their spill taken out - where something stands
        # in front of it (the occlusion of its reference, never a key read on this frame), the margin around the quad
        # with the key's bleed clamped out. In the margin, the footage's key colour past the bezel is filled
        # with the bezel's colour where nothing stands in front of it, and the surface running past the quad is left
        # as the footage shows it, for the residual-key gate (both read once, on the first frame: ``fill.band``).
        showing = visible if visible is not None else np.ones_like(coverage)
        surface_share = coverage * showing
        inner = np.clip(source_alpha / np.maximum(coverage, 1e-3), 0.0, 1.0)
        distance = ndimage.distance_transform_edt(~inside)
        near = distance <= margin
        near_alpha = np.clip((margin + 2.0 - distance) / 2.0, 0.0, 1.0)
        band = fill.band
        bezel_by_side = fill.bezel_sides
        front = screens.despill_clamp(region, key, near & ~band.edge if band is not None else near)
        # Under the source: the filled surface, or what fills the bands a contain fit leaves.
        under, band_fill, band_rgb = _backdrop(fill.flat, banded, picture, drawn_px, key, fill.bezel, light, insert.light)
        # The edge of what stands in front: the key's colour limited out of it, what the clamp leaves of
        # its hue given its neighbours' colour - the subject's own colours left -, the insert's light wrapped
        # onto it.
        edge = edge_of(
            region, foreground, key, edge_reach, drawn=front, subject=insert.subject, declared=insert.despill.spans(), extent=insert.despill.reach(scale, feather)
        )
        surface_shows = lit * inner[..., None] + under * (1.0 - inner)[..., None]
        front, edge_spilled, edge_despilled = treat_edge(
            front,
            region,
            foreground,
            inside,
            key,
            insert.despill,
            edge,
            surface_share < 0.98,
            surface_shows,
            scale=scale,
            feather=feather,
            # The key's share of the edge's pixels, grey once clamped where the occlusion holds them in front - it
            # reaches a few pixels past a dark subject into the key.
            matte=raw_matte,
            clear=clear,
            coverage=coverage,
            core=core,
        )
        if band is not None:
            front = band.fill(front, showing)
        base = front * (1.0 - surface_share)[..., None] + under * (surface_share * (1.0 - inner))[..., None]
        if bloom is not None:
            halo = _halo(bloom, box) * insert.glow
            base = base + halo * (1.0 - alpha)[..., None] - base * halo / 255.0
        rgb = np.clip(base + lit * alpha[..., None], 0.0, 255.0)
        if visibility < 1.0:
            rgb = _partly_shown(rgb, front, surface_share, key, fill.bezel, light, insert.light, visibility)
        image = np.dstack([rgb, np.maximum(coverage, near_alpha) * 255.0])
    elif key is not None and region is not None:
        # The surface's share of each pixel: all of it inside the quad and clear of the foreground (painted flat),
        # the key's own share at the foreground's edge, none outside the quad.
        surface_share = coverage * kept
        # The source's own opacity within the surface (a contain fit's transparency), the quad's edge taken out.
        inner = np.clip(source_alpha / np.maximum(coverage, 1e-3), 0.0, 1.0)
        distance = ndimage.distance_transform_edt(~inside)
        near = distance <= margin
        near_alpha = np.clip((margin + 2.0 - distance) / 2.0, 0.0, 1.0)
        bezel_by_side = bezel_sides(region, matte, inside, distance, margin, RESIDUAL_MARGIN * width, drawn_px - origin, key)
        band = band_of(region, matte, inside, distance, margin, drawn_px - origin, feather, bezel_by_side)
        surfaced = inside & (matte > 0.99) & (soft < 0.01)
        luma = region @ weights
        key_rgb = np.asarray(key.rgb, dtype=np.float32)
        key_luma = float(key_rgb @ weights)
        level = float(np.median(luma[surfaced])) if surfaced.sum() > 32 else key_luma
        scaled_key = key_rgb * (level / max(key_luma, 1.0))
        # What is not the key in each pixel, by colour: the pixel less the key's share it holds - the linear
        # colour-difference share (``screens.key_share``), never the matte, which saturates long before a pixel is
        # all key and read as a share takes too much out, past black, back as the key's complement (the magenta of
        # an inverting despill); and never on a pixel the matte reads as none of the key (another colour
        # the key's channel happens to lead in). Brought back to its own colour and despilled by the clamp: a
        # person's blurred edge is so unmixed rather than cut, and the margin loses the key's bleed.
        unmixed = np.minimum(screens.key_share(region, key), np.clip(matte * 1.5, 0.0, 1.0))
        # In the margin around the quad the key's bleed on the bezel is taken out; a pixel there that is the key
        # itself is either the surface running past the quad - left as it is for the residual-key gate to see - or the
        # footage's own key colour past the bezel, which no quad can cover: filled with the bezel's colour (``band``).
        bleed = inside | (matte < MARGIN_BLEED_MATTE)
        unmixed = np.where(bleed, unmixed, 0.0)
        behind = np.clip(region - unmixed[..., None] * scaled_key, 0.0, None)
        unclamped = np.clip(behind / np.maximum(1.0 - unmixed, 0.2)[..., None], 0.0, 255.0)
        foreground_colour = screens.despill_clamp(unclamped, key, near & bleed)
        # Under the source: the keyed footage with its key taken out where the source is transparent, or what fills
        # the bands a contain fit leaves - never the surface there.
        flat = screens.despill_clamp(region, key, near)
        under, band_fill, band_rgb = _backdrop(
            flat, banded, picture, drawn_px, key, lambda: bezel_colour(region, matte, inside, distance, margin, key), light, insert.light
        )
        # The edge of what stands in front: the key's colour limited out of it, what the clamp leaves of
        # its hue given its neighbours' colour - the subject's own colours left -, the insert's light wrapped
        # onto it.
        edge = edge_of(
            region,
            foreground,
            key,
            edge_reach,
            drawn=foreground_colour,
            subject=insert.subject,
            declared=insert.despill.spans(),
            extent=insert.despill.reach(scale, feather),
        )
        surface_shows = lit * inner[..., None] + under * (1.0 - inner)[..., None]
        foreground_colour, edge_spilled, edge_despilled = treat_edge(
            foreground_colour,
            unclamped,
            foreground,
            inside,
            key,
            insert.despill,
            edge,
            surface_share < 0.98,
            surface_shows,
            scale=scale,
            feather=feather,
            # The key's share the unmix left in the edge's pixels - its cap is the matte's, which the window holds in
            # front past what this frame shows - grey once clamped: read at this frame's own key share.
            matte=raw_matte,
            clear=clear,
            coverage=coverage,
            core=core,
        )
        foreground_colour = band.fill(foreground_colour)
        # The pixel: what is not surface keeps its own colour; the surface shows the insert where the source is
        # opaque (``alpha``, below) and what is under it where it is not.
        base = foreground_colour * (1.0 - surface_share)[..., None] + under * (surface_share * (1.0 - inner))[..., None]
        if bloom is not None:
            # The layer covers the keyed surface whole, so the glow the page draws under it never shows there:
            # its light falls on that surface here, as the page's screen blend would lay it.
            halo = _halo(bloom, box) * insert.glow
            base = base + halo * (1.0 - alpha)[..., None] - base * halo / 255.0
        rgb = np.clip(base + lit * alpha[..., None], 0.0, 255.0)
        if visibility < 1.0:
            rgb = _partly_shown(
                rgb,
                foreground_colour,
                surface_share,
                key,
                lambda: bezel_colour(region, matte, inside, distance, margin, key),
                light,
                insert.light,
                visibility,
            )
        image = np.dstack([rgb, np.maximum(coverage, near_alpha) * 255.0])
    else:
        image = np.dstack([lit, alpha * visibility * 255.0])
    # 4b. The layer's margin. Within RESIDUAL_MARGIN of the quad the key's hue takes the bezel's colour on
    # its side - the bezel the quad is snapped to, never a grey -, but for the surface running past a quad too small,
    # which no margin hides: left for the residual-key gate. Past it, the footage's own key colour - its screen's glow,
    # a spill on the desk - is despilled toward the luminance around it, never a finding. What the layer took out of
    # the margin (the clamp, the bezel's fills, this) is reported.
    residual_reach = max(1.0, RESIDUAL_MARGIN * width)
    despilled = None
    margin_ring = None
    if near is not None and distance is not None:
        margin_ring = ~inside & (distance <= residual_reach)
        filling = margin_ring & ~running_past(band.edge, distance, residual_reach) if band is not None else margin_ring
        rgb, _filled = fill_margin(image[..., :3], key, filling, bezel_by_side, drawn_px - origin, scale)
        rgb = _despill_margin(rgb, key, near & ~inside & (distance > residual_reach), scale)
        image = np.dstack([rgb, image[..., 3]])
        ring = near & ~inside
        # The layer's pixels read only where the footage's are of the key's hue: the rest is never despilled.
        hued_before = _key_hued(region, key, ring)
        despilled = hued_before & ~_key_hued(image[..., :3], key, hued_before)
    # 4c. The other inserts' quads. Past a bezel narrower than the margin, the layer's margin and its fade reach into a
    # neighbouring insert's quad: drawn over it, they leave the footage's key as a line at that insert's edge and its
    # last pixels in this one's bezel colour - which neither insert's audit sees. Their surfaces are theirs: cut out.
    theirs = others_cut(insert.others, inside, box, (width, height))
    cut_share = None
    if theirs is not None:
        reached = image[..., 3] > 0
        cut_share = round(float((theirs & reached).sum()) / max(float(inside.sum()), 1.0), 5) or None
        image[..., 3] = np.where(theirs, 0.0, image[..., 3])
    layer_image = Image.fromarray(np.clip(image, 0, 255).astype(np.uint8), "RGBA")

    # Partly shown: the surface is judged - its key, its bezel, its edge -, never what the fade does to the insert.
    whole = visibility >= 1.0
    audit = FrameAudit(edge_hold=overlap, visibility=None if whole else round(float(visibility), 3), others_cut=cut_share)
    if insert.fit == "cover":
        # A cover fit cutting the source's own content to fill the quad's shape: a sidebar, a column goes.
        quad_w, quad_h = quad_size(tuple(map(tuple, drawn_px - origin)))
        cut = cover_cuts(picture.size, content_box(picture), quad_w, quad_h)
        audit.cropped = cut if cut > 0 else None
    quad_pixels = int(inside.sum())
    if banded is not None and quad_pixels:
        # The bands a contain fit left and what filled them: unkeyed, or keyed with no footage to key, the
        # page shows the footage there.
        audit.band_share = round(float(banded.sum()) / quad_pixels, 4)
        audit.band_fill = band_fill if band_fill is not None else "footage"
        audit.band_colour = format_hex(tuple(round(v) for v in band_rgb)) if band_rgb is not None else None
        if audit.band_fill == "footage" and region is not None:
            # Left to the footage: a chroma surface shows its key colour there, never the fit's design.
            keyed_band = band_key_colour(region, banded)
            if keyed_band is not None:
                audit.band_key, audit.band_key_share = keyed_band
    audit.edge_despill = edge_despilled
    audit.edge_spill = edge_spilled
    over = None
    through_hits = None
    matte_hits = None
    spill = None
    if foreground is not None and quad_pixels:
        if core is None:
            core = front_core(foreground, inside, reach)
        # The insert drawn where the mask says something stands in front, at any share of it a viewer sees.
        over = (alpha > DRAWN_MIN_ALPHA) & core
        audit.covered = round(float(over.sum()) / quad_pixels, 4)
        # The quad's edge and its antialiased pixels drawn over the subject crossing it: the core leaves that band
        # out, so it is read apart, against the key's own reading of the subject (#11460).
        if key is not None and raw_matte is not None:
            edge_share, edge_hits = edge_line(
                alpha, foreground & inside, raw_matte, coverage, drawn_px - origin, scale, reach
            )
            audit.edge_line = edge_share or 0.0
            if edge_hits.any():
                over = over | edge_hits
        if (foreground & inside).any():
            audit.front_confidence = confidence
    if insert.logo and whole:
        own = float(source_alpha.sum())
        audit.logo_visible = round(float(alpha.sum()) / own, 4) if own > 0 else 0.0
    if region is not None and quad_pixels:
        judged = inside & (source_alpha >= 0.5)
        if core is not None:
            judged &= ~core
        audit.size = [width, height]
        audit.alignment = round(alignment, 5) if alignment is not None else None
        if locked is not None and locked.held:
            audit.held = True
            audit.alignment_found = round(locked.found_error, 5) if locked.found_error is not None else None
        audit.refined = [[round(x, 5), round(y, 5)] for x, y in refined_quad]
        near_switch = getattr(insert.track, "near_switch", None)
        if near_switch is not None and near_switch(footage_frame):
            audit.track_switch = True
        # The bezel lock's reading of the frame: each side's offset, and what each edge's points say of the
        # surface's flatness - a filled or locked insert's read once, on its first frame.
        reading = fill.found if filled else held_lock.found if held_lock is not None else locked
        if reading is not None:
            audit.edge_bows = list(reading.bows)
            audit.edge_inliers = list(reading.inliers)
            audit.edge_residuals = list(surface_refine.shares_of(reading.residuals, width))
            audit.edge_visible = list(reading.visible)
            if alignment is not None:
                audit.alignment_sides = _side_alignment(found_px, quad_px, width, reading.edges_found)
        if drift is not None and drift_px is not None:
            audit.alignment_drift = round(drift, 5)
            audit.drift_sides = _side_alignment(drift_px, found_px, width, drift_read.edges_found)
            audit.bezel_found = [[round(float(x) / width, 5), round(float(y) / height, 5)] for x, y in drift_px]
        a = image[..., 3:4] / 255.0
        composed = image[..., :3] * a + region * (1.0 - a)
        # The source's own strokes drawn across what stands in front, read in the frame's pixels: on the core
        # the insert is never drawn on - the part of it the key reads clear on a keyed insert, never the key's residue
        # the layer draws as the insert there.
        if whole and core is not None and core.any():
            judged_front = core & (raw_matte < KEY_CLEAR_MATTE) if key is not None and raw_matte is not None else core
            audit.through, through_hits = through_share(composed, region, shown, judged_front, scale)
            if audit.through_foreground and through_hits is not None and foreground is not None:
                audit.through_edge = on_edge(through_hits, foreground, scale)
        audit.matte_refined = True if refine else None
        # An unkeyed insert's mask past its subject: the surface left showing inside it, and its lag.
        if whole and key is None and foreground is not None:
            matte = matte_read(region, alpha, foreground, inside, scale, width * height)
            audit.matte_halo, audit.matte_halo_region, audit.matte_lag = matte.halo, matte.halo_region, matte.lag
            matte_hits = matte.hits
        # The key's hue past the quad on the whole footage frame: what no margin of the layer reaches.
        if whole and key is not None:
            spill = key_spill(footage, key, drawn_px, insert.others, subject=insert.subject, declared=insert.despill.spans())
            audit.key_spill, audit.key_spill_box = spill.share, spill.box
            audit.key_spill_side, audit.key_spill_to = spill.joined, spill.reaches
        # What every gate reads, once: the source's own key hue, the filled contain bands, what stands in front
        # grown past its edge ring, its share of the quad, the margin.
        masks = gate_masks(
            shown,
            source_alpha,
            key,
            inside,
            contain_band=banded if band_fill in FILLED_BANDS else None,
            foreground=foreground,
            margin=margin_ring,
            scale=scale,
            feather=feather,
        )
        clean = masks.clean(inside, source_alpha)
        audit.foreground_share = masks.coverage
        audit.judged_share = round(float(clean.sum()) / quad_pixels, 4)
        # The quad as composited - the footage under a matte that does not take, the light laid over it - held to the
        # source drawn into the same pixels, where the source is opaque, clear of what stands in front and of the
        # contain bands; too much of it covered, nothing is left to judge: said, not compared.
        if whole and masks.coverage is not None and masks.coverage > COLOUR_MAX_FOREGROUND:
            audit.colour_skipped = True
        elif whole:
            for name, value in (colour_fidelity(composed, shown, clean) or {}).items():
                setattr(audit, name, value)
        showing = judged & (alpha >= 0.5)
        audit.coverage = round(float(alpha[judged].mean()), 4) if judged.any() else 0.0
        if whole and showing.any():
            audit.luma = round(float((composed @ weights)[showing].mean()), 2)
            audit.source_luma = round(float((shown @ weights)[showing].mean()), 2)
        if whole:
            # A video source changes every frame: its sharpness reference is read each time; a picture's is kept.
            audit.sharpness = _sharpness(
                composed @ weights,
                clean & (alpha >= 0.5),
                picture,
                insert.fit,
                drawn_px - origin,
                state if insert.kind != "video" else None,
            )
        if key is not None:
            hsv = _hsv(composed)
            audit.source_key_hue = round(float((masks.source_keyhue & inside).sum()) / quad_pixels, 5)
            # Inside the quad and RESIDUAL_MARGIN around it, no farther: past it the footage is its own. A seam
            # of the key's hue straight across the quad is counted whatever mask it falls in: the source's own
            # key hue is only what the source drawn at that place shows.
            zone = masks.residual_zone(inside)
            keyed = _hue_outliers(hsv, key, complement=False, saturation=RESIDUAL_MIN_SATURATION)
            if theirs is not None:
                # Another insert's surface is that insert's to cover, never this one's residual key.
                zone &= ~theirs
                keyed &= ~theirs
            seams = key_seams(keyed & ~masks.source_own, inside, scale)
            if seams.any():
                zone = zone | seams
                audit.residual_seam = round(float(seams.sum()) / quad_pixels, 5)
            audit.residual_key, left = _residual_key(hsv, key, zone, quad_pixels)
            # A thin line of the key along a side, inside the quad or just past it: its share never fails a frame.
            edged = key_edge_lines(keyed & zone, drawn_px - origin, scale)
            if edged:
                audit.residual_edge = edged
            # The key left in a pocket inside what stands in front, where the insert is not drawn: the key as
            # the matte the frame was drawn with reads it (before any despill) or its hue as composed (after it).
            read = matte if matte is not None else raw_matte
            if foreground is not None and read is not None:
                pocketed = key_pockets(foreground, inside, (read >= POCKET_KEY_MATTE) & (alpha < 0.5), reach)
                if pocketed.any():
                    audit.residual_pocket = round(float(pocketed.sum()) / quad_pixels, 5)
                    audit.residual_key = round(float(audit.residual_key or 0.0) + audit.residual_pocket, 5)
                    left = pocketed if left is None else (left | pocketed)
            if left is not None:
                past = band.edge if band is not None else None
                audit.residual_where = _residual_where(left, inside, past, drawn_px - origin, quad_pixels, banded)
            source_hue = masks.source_keyhue | masks.source_complement
            haloed = _halo_share(hsv, foreground, inside, key, scale, own=edge, source=source_hue)
            audit.halo, audit.halo_own, audit.halo_edge = haloed.share, haloed.own, haloed.edge
            audit.halo_clamped, audit.halo_hue = haloed.clamped, haloed.hue
            if evidence and audit.haloed:
                halo_crop = _halo_crop(composed, hsv, foreground, inside, key, scale, own=edge, source=source_hue)
            # A grey band past the subject is no hue outlier: read apart, where the source is opaque and not of
            # the key's hue - what stands in front's core included, where the occlusion holds the key's residue.
            # Read on the keyed surface alone, against the footage before any despill: the footage's own grey
            # surroundings the quad overlaps are a fact, never the key's residue.
            judged_rim = inside & (source_alpha >= 0.5) & ~source_hue
            rim = _rim_share(composed, shown, lit, alpha, clear, judged_rim, key, scale, footage=region, matte=raw_matte)
            audit.rim, audit.rim_edge, audit.rim_light = rim.share, rim.edge, rim.light
            audit.rim_surroundings, audit.rim_whole, audit.rim_off = rim.surroundings, rim.whole, rim.off
            if evidence and audit.rimmed and rim.hits is not None:
                rim_crop = _crop_around(composed, rim.hits, scale, max(1, math.ceil(RIM_FROM_PX * scale)))
            if despilled is not None and despilled.any():
                audit.despilled = round(float(despilled.sum()) / quad_pixels, 5)
                audit.despilled_where = _side_shares(despilled, drawn_px - origin, quad_pixels)
    proof = None
    residual_crop = None
    foreground_crop = None
    if evidence and audit.keyed_residue and audit.residual_where:
        residual_crop = _residual_crop(composed, audit.residual_where, drawn_px - origin, margin)
    through_crop = spill_frame = None
    if evidence and (audit.covers_foreground or audit.draws_edge_line) and over is not None and region is not None:
        foreground_crop = _front_crop(composed, over, scale)
    if evidence and audit.through_foreground and through_hits is not None and through_hits.any():
        through_crop = _crop_around(composed, through_hits, scale, max(1, math.ceil(THROUGH_TILE_PX * scale)))
    if evidence and audit.spills_key and spill is not None:
        spill_frame = spill.picture(footage, composed, box)
    if evidence and audit.failing:
        base_frame = region if region is not None else np.zeros((y1 - y0, x1 - x0, 3), dtype=np.float32)
        a = image[..., 3:4] / 255.0
        shown_frame = image[..., :3] * a + base_frame * (1 - a)
        if (audit.covers_foreground or audit.draws_edge_line) and over is not None:
            shown_frame[over] = shown_frame[over] * 0.4 + np.array([255.0, 0.0, 0.0]) * 0.6
        if audit.through_foreground and through_hits is not None:
            shown_frame[through_hits] = shown_frame[through_hits] * 0.4 + np.array([255.0, 0.0, 0.0]) * 0.6
        if audit.matte_haloed and matte_hits is not None:
            shown_frame[matte_hits] = shown_frame[matte_hits] * 0.4 + np.array([255.0, 0.0, 0.0]) * 0.6
        proof = Image.fromarray(np.clip(shown_frame, 0, 255).astype(np.uint8), "RGB")
        if audit.misaligned:
            _outline(proof, quad_px - origin, (255, 60, 60))
            _outline(proof, drawn_px - origin, (80, 255, 120))
            if drift_px is not None:
                _outline(proof, drift_px - origin, (255, 210, 60))
    return Layer(
        Warped(box, layer_image),
        bloom,
        audit,
        proof,
        residual_crop,
        halo_crop,
        foreground_crop,
        rim_crop,
        through_crop,
        spill_frame,
        refinable=key is not None and region is not None and not filled and not refine,
    )


def on_edge(hits: Any, foreground: Any, scale: float) -> bool:
    """Whether every pixel of *hits* (:func:`through_share`'s) lies on the edge of *foreground*: within
    :data:`REFINED_MATTE_PX` and a tile (:data:`THROUGH_TILE_PX`) of pixels of a 1080p frame of its boundary - what
    the refined matte reaches (#10573)."""
    import numpy as np
    from scipy import ndimage

    found = np.asarray(hits, dtype=bool)
    if not found.any():
        return False
    depth = ndimage.distance_transform_edt(np.asarray(foreground, dtype=bool))
    return bool((depth[found] <= (REFINED_MATTE_PX + THROUGH_TILE_PX) * scale).all())


def drawn_on(first: Any, quad: Any, key: KeySpec, despill: Despill | None = None) -> FrameAudit | None:
    """A static keyed insert drawn on *quad* (0-1) of *first* - the footage frame it comes in on, ``H x W x 3`` uint8 at
    its own size - as the frame server draws it (:func:`static_fill`, :func:`compose`), a flat stand-in for its source:
    that frame's audit. Its alignment and its residual key say whether the insert covers the surface - what a
    save reads of a bowed one, once, at the footage's resolution. ``None`` when nothing is drawn there."""
    import numpy as np
    from PIL import Image

    pixels = np.ascontiguousarray(np.asarray(first)[..., :3], dtype=np.uint8)
    height, width = pixels.shape[:2]
    corners = tuple((float(x), float(y)) for x, y in quad)
    keyed = key.sampled(pixels, [(x * width, y * height) for x, y in corners])
    insert = InsertSource("", "image", corners, key=keyed, footage=Footage("", "image"), despill=despill or Despill())
    fill = static_fill(insert, pixels, (width, height))
    layer = compose(insert, Image.new("RGB", (16, 9), (128, 128, 128)), (width, height), 0, footage=pixels, fill=fill)
    return layer.audit if layer is not None else None


def _outline(picture: Any, quad_px: Any, colour: tuple[int, int, int]) -> None:
    """*quad_px* drawn on *picture* (RGB, in place): the given quad in red, the refined one in green, on a finding's frame."""
    from PIL import ImageDraw

    points = [tuple(float(v) for v in p) for p in quad_px]
    ImageDraw.Draw(picture).line([*points, points[0]], fill=colour, width=2)


def _foreground(nonkey: Any, inside: Any, scale: float) -> Any:
    """What stands in front of a keyed surface (boolean, the box's shape): the non-key regions of *inside* that
    reached it from outside.

    A connected non-key region that reaches the surface's rim came in from
    outside it - a person, a hand -, one holding :data:`FOREGROUND_MIN_SHARE`
    of the quad stands inside it; either must reach deeper than the blur of
    the surface's own edge. A smaller region closed inside the surface is
    what the model drew on the screen, and is covered.
    """
    import numpy as np
    from scipy import ndimage

    candidate = ndimage.binary_opening(nonkey & inside, iterations=1)
    labels, count = ndimage.label(candidate)
    nothing = np.zeros(inside.shape, dtype=bool)
    if count == 0:
        return nothing
    rim = inside & ~ndimage.binary_erosion(inside, iterations=3)
    deep = ndimage.binary_erosion(inside, iterations=max(2, math.ceil(4.0 * scale)))
    sizes = ndimage.sum(candidate, labels, range(1, count + 1))
    reaching = set(np.unique(labels[rim & candidate]).tolist()) - {0}
    big = {index + 1 for index, size in enumerate(sizes) if size >= FOREGROUND_MIN_SHARE * float(inside.sum())}
    into = set(np.unique(labels[deep & candidate]).tolist()) - {0}
    kept = [index for index in reaching | big if index in into]
    if not kept:
        return nothing
    return np.isin(labels, kept)


def key_pockets(foreground: Any, inside: Any, keyed: Any, reach: int) -> Any:
    """The pockets of the key inside what stands in front (boolean, the box's shape): the connected regions of
    *keyed* within *foreground* and the quad (*inside*) deeper than the matte's soft edge - something of them left once
    opened *reach* pixels, never a fringe a strand wide - and of :data:`RESIDUAL_MIN_PIXELS` at least. A pocket between a
    subject and the bezel's corner, closed into one body with the subject by the occlusion's mask, is one of them."""
    import numpy as np
    from scipy import ndimage

    nothing = np.zeros(np.shape(inside), dtype=bool)
    candidate = np.asarray(foreground, dtype=bool) & np.asarray(inside, dtype=bool) & np.asarray(keyed, dtype=bool)
    if candidate.sum() < RESIDUAL_MIN_PIXELS:
        return nothing
    deep = ndimage.binary_opening(candidate, iterations=max(1, int(reach)))
    if not deep.any():
        return nothing
    labels, count = ndimage.label(candidate)
    sizes = ndimage.sum(candidate, labels, range(1, count + 1))
    kept = [index for index in set(np.unique(labels[deep]).tolist()) - {0} if sizes[index - 1] >= RESIDUAL_MIN_PIXELS]
    return np.isin(labels, kept) if kept else nothing


def key_out_pockets(visible: Any, foreground: Any, inside: Any, matte: Any, reach: int) -> tuple[Any, Any]:
    """*visible* and *foreground* (an occlusion's, over the box) with the pockets of the key inside what stands in front
    (:func:`key_pockets`, read on the frame's own key *matte* at :data:`POCKET_KEY_MATTE`) given to the surface:
    the insert is drawn there."""
    import numpy as np

    pocket = key_pockets(foreground, inside, np.asarray(matte) >= POCKET_KEY_MATTE, reach)
    if not pocket.any():
        return visible, foreground
    return np.where(pocket, 1.0, visible), foreground & ~pocket


def front_core(foreground: Any, inside: Any, reach: int) -> Any:
    """What stands in front inside the quad less its edge ring, *reach* pixels deep (twice a key's feather; an
    occlusion's feather): where the insert is never drawn, and where the gate counts it drawn
    (``FrameAudit.covered``). The ring keeps the matte's soft edge - a keyed edge the key's share of each pixel."""
    from scipy import ndimage

    return ndimage.binary_erosion(foreground & inside, iterations=max(1, int(reach)))


def front_past_quad(foreground: Any, nonkey: Any, inside: Any, grow: int) -> Any:
    """*foreground* (what stands in front, read inside the quad: :func:`_foreground`) grown past the quad's edge through
    the footage's *nonkey* pixels, *grow* pixels at most (#11460).

    Read inside the quad alone, a subject crossing the quad's edge ends there: its feather and its core
    (:func:`front_core`) then take the quad's edge for the subject's own, and the insert is drawn at part of its
    opacity along that edge - its antialiased pixels included - over the subject, a thin line across a presenter
    standing in front of the screen's side. Grown past it, the quad's edge is clipped by the same matte as the
    insert's body; nowhere else does it change: a quad edge no subject crosses is never reached.
    """
    import numpy as np
    from scipy import ndimage

    front = np.asarray(foreground, dtype=bool)
    if not front.any():
        return front
    near = ndimage.binary_dilation(front, iterations=max(1, int(grow)))
    return front | (near & np.asarray(nonkey, dtype=bool) & ~np.asarray(inside, dtype=bool))


def edge_line(
    drawn: Any, foreground: Any, matte: Any, coverage: Any, quad_px: Any, scale: float, reach: int
) -> tuple[float, Any]:
    """The quad's edge drawn as a line over what stands in front (#11460): ``(share, hits)`` - the share of the quad's
    outline (*quad_px*, pixels of the box) where the insert is drawn, past :data:`DRAWN_MIN_ALPHA` of *drawn* (the
    surface's share of each pixel), on the subject's pixels of the quad's edge band, and those pixels (boolean).

    The edge band: the quad's antialiased pixels (*coverage* strictly between 0 and 1) and :data:`EDGE_LINE_PX`
    pixels of a 1080p frame (*scale*) either side. The subject's pixels there: the key reads them clear
    (*matte* under :data:`KEY_CLEAR_MATTE`), within reach of what stands in front (*foreground*, read inside the quad),
    its own soft edge left out - *reach* pixels deep, the quad's edge never taken for the subject's. A quad edge no
    subject crosses holds none: the share is 0.
    """
    import numpy as np
    from scipy import ndimage

    front = np.asarray(foreground, dtype=bool)
    cover = np.asarray(coverage, dtype=np.float32)
    if not front.any():
        return 0.0, np.zeros(front.shape, dtype=bool)
    inside = cover > 0.5
    width = max(1, math.ceil(EDGE_LINE_PX * scale))
    band = ndimage.binary_dilation((cover > 0.0) & (cover < 1.0), iterations=width) & (cover > 0.0)
    held = ndimage.binary_erosion(front | ~inside, iterations=max(1, int(reach)))
    held &= ndimage.binary_dilation(front, iterations=1) & (np.asarray(matte) < KEY_CLEAR_MATTE)
    hits = band & held & (np.asarray(drawn, dtype=np.float32) > DRAWN_MIN_ALPHA)
    corners = np.asarray(quad_px, dtype=float).reshape(4, 2)
    outline = float(np.hypot(*(np.roll(corners, -1, axis=0) - corners).T).sum())
    if not hits.any() or outline <= 0.0:
        return 0.0, hits
    # The pixels drawn over the subject per pixel of the quad's outline: a 1 px line along a tenth of it reads 0.1.
    return round(float(hits.sum()) / outline, 5), hits


def guided_matte(guide: Any, opacity: Any, radius: int, eps: float = REFINED_MATTE_EPS) -> Any:
    """*opacity* (``H x W`` float, 0-1: how much of each pixel stands in front) refined by a guided filter on *guide*
    (the footage's luma, 0-255): each pixel's opacity a linear function of the footage's luma over the *radius*
    window around it (He et al., 2010), so the matte's edge follows the edge the footage shows - a finger's, a
    muzzle's - rather than the key's soft band. *eps* (on the luma normalised to 0-1) is how far a flat window
    smooths rather than follows. Box means over ``2 * radius + 1`` pixels; clipped to 0-1."""
    import numpy as np
    from scipy import ndimage

    size = 2 * max(1, int(radius)) + 1
    i = np.asarray(guide, dtype=np.float64) / 255.0
    p = np.asarray(opacity, dtype=np.float64)

    def mean(values: Any) -> Any:
        return ndimage.uniform_filter(values, size=size, mode="nearest")

    mean_i, mean_p = mean(i), mean(p)
    var_i = mean(i * i) - mean_i * mean_i
    cov_ip = mean(i * p) - mean_i * mean_p
    a = cov_ip / (np.maximum(var_i, 0.0) + eps)
    b = mean_p - a * mean_i
    return np.clip(mean(a) * i + mean(b), 0.0, 1.0).astype(np.float32)


def refined_front(region: Any, opacity: Any, foreground: Any, inside: Any, core: Any, scale: float) -> tuple[Any, Any]:
    """The keyed matte refined on the edge of what stands in front (#10573): ``(opacity, core)``.

    *opacity* (``soft * (1 - matte)``: the subject's share of each pixel)
    is refined by :func:`guided_matte` on the footage's luma (*region*)
    within :data:`REFINED_MATTE_PX` pixels of a 1080p frame of the
    *foreground*'s boundary inside the quad (*inside*), never lowered
    there: where the footage shows the subject the edge is as opaque as the
    footage says. Its pixels past :data:`REFINED_OPAQUE` join *core*
    (:func:`front_core`), where the insert is never drawn."""
    import numpy as np
    from scipy import ndimage

    reach = max(1.0, REFINED_MATTE_PX * scale)
    front = np.asarray(foreground, dtype=bool) & inside
    if not front.any():
        return opacity, core
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    refined = guided_matte(np.asarray(region, dtype=np.float32) @ weights, opacity, math.ceil(reach))
    near = ndimage.distance_transform_edt(~front) <= reach
    near &= ndimage.distance_transform_edt(front) <= reach + 1.0
    band = near & inside
    raised = np.where(band, np.maximum(opacity, refined), opacity).astype(np.float32)
    grown = np.asarray(core, dtype=bool) | (band & (raised >= REFINED_OPAQUE))
    return raised, grown


def in_front(share: Any, core: Any) -> Any:
    """*share* - the share of each pixel the matte gives the surface, the insert drawn at it - with none where what
    stands in front holds (*core*: :func:`front_core`): what the mask marks as in front is never drawn over."""
    import numpy as np

    return np.where(core, 0.0, share)


def key_confidence(matte: Any, foreground: Any, inside: Any) -> float | None:
    """How sure a keyed mask of what stands in front is (``FrameAudit.front_confidence``): the share of it inside
    the quad whose matte reads clear of the key (:data:`KEY_CLEAR_MATTE`); ``None`` with nothing in front there."""
    front = foreground & inside
    count = int(front.sum())
    if not count:
        return None
    return round(float((front & (matte < KEY_CLEAR_MATTE)).sum()) / count, 4)


@dataclass(frozen=True)
class Clear:
    """The part of what stands in front of a keyed surface its key reads clear on one frame (:func:`clear_part`):
    the subject itself, told from the surface by the key alone - never the key's residue the mask holds in front."""

    #: The window of the box it and the :data:`RIM_PX` around it lie in, and over that window: the part (boolean), each
    #: pixel's distance to it in pixels, and the nearest pixel of it (``(rows, columns)``, in the window).
    window: tuple[slice, slice]
    part: Any
    distance: Any
    nearest: Any
    #: Its edge inside the quad, in pixels.
    edge: int

    def zone(self, scale: float) -> Any:
        """The pixels of the window past the part and within :data:`RIM_PX` of it (*scale*: the frame's height over 1080)."""
        return ~self.part & (self.distance <= RIM_PX * scale)


def clear_part(foreground: Any, inside: Any, matte: Any, scale: float) -> Clear | None:
    """The :class:`Clear` part of *foreground* (boolean over the box) inside the quad (*inside*): its pixels the frame's
    own key *matte* reads under :data:`KEY_CLEAR_MATTE`. ``None`` with no matte, or nothing in front clear of the key."""
    import numpy as np
    from scipy import ndimage

    if foreground is None or matte is None:
        return None
    part = np.asarray(foreground, dtype=bool) & inside & (np.asarray(matte) < KEY_CLEAR_MATTE)
    window = _around(part, math.ceil(RIM_PX * scale) + 2)
    if window is None:
        return None
    inner = part[window]
    distance, nearest = ndimage.distance_transform_edt(~inner, return_indices=True)
    edge = int((inner & ~ndimage.binary_erosion(inner, iterations=1, border_value=1)).sum())
    return Clear(window, inner, distance, nearest, edge)


def key_residue(colours: Any, shows: Any, matte: Any, clear: Clear | None, coverage: Any, scale: float) -> Any:
    """*colours* (``H x W x 3`` float: the footage as a keyed layer draws it, unmixed, clamped and its edge treated) with
    the key's residue on the edge of what stands in front drawn as the insert.

    The clamp (``screens.despill_clamp``) turns the key's share of a pixel
    it was left in - by an unmix capped by a matte the window holds in front,
    or by no unmix at all where an occlusion holds the pixel in front - into
    a grey, dark on a dark edge, that the footage never had: no hue outlier,
    so no halo, and the edge treatment's light wrap reaches two pixels of
    it. So within :data:`RIM_PX` of the part of what stands in front the key
    reads clear (*clear*, :func:`clear_part`), inside the quad (*coverage*),
    a pixel the frame's own key *matte* reads part key (past
    :data:`KEY_CLEAR_MATTE`) whose colour has no chroma left (under
    ``screens.MIN_HUE_CHROMA``, fully from half of it) takes the insert's own
    pixel (*shows*: what the surface shows there) instead, blended by that
    matte - all of it where the matte reads the key whole: the band reads as
    the insert behind a soft edge. The subject's own colours have chroma, and
    its part the key reads clear is never touched.
    """
    import numpy as np

    from app.services.video_studio import screens

    if clear is None or matte is None or shows is None:
        return colours
    window = clear.window
    keyed = np.asarray(matte, dtype=np.float32)[window]
    covered = np.asarray(coverage, dtype=np.float32)[window]
    zone = clear.zone(scale) & (keyed > KEY_CLEAR_MATTE) & (covered > 0.0)
    if not zone.any():
        return colours
    out = np.array(colours, dtype=np.float32, copy=True)
    drawn = out[window]
    pixels = drawn[zone]
    ycc = screens.to_ycc(pixels)
    grey = np.clip((screens.MIN_HUE_CHROMA - np.hypot(ycc[:, 1], ycc[:, 2])) / (0.5 * screens.MIN_HUE_CHROMA), 0.0, 1.0)
    share = np.clip((keyed[zone] - KEY_CLEAR_MATTE) / (1.0 - KEY_CLEAR_MATTE), 0.0, 1.0) * grey * np.clip(covered[zone], 0.0, 1.0)
    drawn[zone] = pixels + (np.asarray(shows, dtype=np.float32)[window][zone] - pixels) * share[:, None]
    return out


@dataclass(frozen=True)
class RimRead:
    """What the grey rim reading found on one frame (:func:`_rim_share`); ``None`` for what it did not read."""

    #: The share of the band over the keyed surface grey where the insert is not - what the gate reads ...
    share: float | None = None
    #: ... the edge of the part the key reads clear, in pixels ...
    edge: int | None = None
    #: ... the share of the rim the footage's light laid over the insert made grey (``None``: no rim) ...
    light: float | None = None
    #: ... the rim's pixels over the keyed surface (boolean over the box; ``None``: none), for its crop ...
    hits: Any = None
    #: ... and, as facts never gated, the same share read on the band off the keyed surface - the footage's own
    #: surroundings the quad overlaps, a desk, a console - and on the whole band (``None``: under 64 pixels there).
    surroundings: float | None = None
    whole: float | None = None
    #: ... and, gated, the share of the band off the keyed surface the compositing made grey: the footage had colour
    #: there (``None``: no matte or no footage to tell, or under 64 pixels there).
    off: float | None = None


def _rim_part(grey: Any, where: Any) -> float | None:
    """The share of *where* (boolean over the band) *grey* holds; ``None`` under 64 pixels of it."""
    count = int(where.sum())
    if count < 64:
        return None
    return round(float((grey & where).sum()) / count, 4)


def _rim_share(
    composed: Any,
    own: Any,
    lit: Any,
    drawn: Any,
    clear: Clear | None,
    judged: Any,
    key: Any,
    scale: float,
    *,
    footage: Any = None,
    matte: Any = None,
) -> RimRead:
    """The key's residue on the edge of what stands in front, read on the composited frame: a grey band no hue
    gate counts - the halo reads the key's hue and its complement.

    The band runs :data:`RIM_FROM_PX` to :data:`RIM_PX` pixels (at *scale*)
    on the surface side of the part the key reads clear (*clear*), where
    *judged* (the source opaque and not of the key's hue): its pixels of
    *composed* with no chroma (under ``screens.MIN_HUE_CHROMA``) where the
    source drawn there (*own*) has some, their luma between the subject's -
    its nearest pixel the key reads clear - and the source's within
    :data:`RIM_LUMA_MARGIN`, are rim; the span reaches the luma of *key*
    clamped (``screens.despill_clamp``) too, the residue's own: a green key's
    weak channels, darker than most dark edges. Of those, the ones the insert
    is drawn on (*drawn*, its alpha, at least a half) whose source the
    footage's light made grey (*lit*) are the light's. ``None`` share with
    no part clear of the key, or a band under 64 pixels.

    The key's residue only lies where the footage showed the key:
    the share gated is read on the band's pixels the frame's own key *matte*
    reads part key (past :data:`KEY_CLEAR_MATTE`) - the keyed surface -, and a
    pixel the *footage* (the frame's own pixels, before any despill) already
    shows grey is never rim: the compositing did not make it so. The band off
    that surface - the footage's own surroundings the quad overlaps, a grey
    desk or console - is read as a fact (``surroundings``), with the whole
    band's share as it was read before (``whole``), never gated. Without a
    *matte* the whole band is the surface. Of that band off the surface, the
    pixels grey in *composed* where the *footage* had colour are the
    compositing's own grey - a subject's fringe the key's clamp turned grey
    over the insert - and gated (``off``), the footage's own grey never.
    """
    import numpy as np

    from app.services.video_studio import screens

    if clear is None:
        return RimRead()
    window = clear.window
    band = np.asarray(judged, dtype=bool)[window] & ~clear.part
    band &= (clear.distance > RIM_FROM_PX * scale) & (clear.distance <= RIM_PX * scale)
    if int(band.sum()) < 64:
        return RimRead(edge=clear.edge)
    seen = np.asarray(composed, dtype=np.float32)[window]
    seen_ycc = screens.to_ycc(seen[band])
    own_ycc = screens.to_ycc(np.asarray(own, dtype=np.float32)[window][band])
    lit_ycc = screens.to_ycc(np.asarray(lit, dtype=np.float32)[window][band])
    rows, columns = clear.nearest[0][band], clear.nearest[1][band]
    subject = screens.to_ycc(seen[rows, columns])[:, 0]
    residue = float(screens.to_ycc(screens.despill_clamp(np.asarray(key.rgb, dtype=np.float32), key))[0, 0])
    luma, source = seen_ycc[:, 0], own_ycc[:, 0]
    low = np.minimum(np.minimum(subject, source), residue) - RIM_LUMA_MARGIN
    high = np.maximum(np.maximum(subject, source), residue) + RIM_LUMA_MARGIN
    between = (luma >= low) & (luma <= high)
    grey = (
        (np.hypot(seen_ycc[:, 1], seen_ycc[:, 2]) < screens.MIN_HUE_CHROMA)
        & (np.hypot(own_ycc[:, 1], own_ycc[:, 2]) >= screens.MIN_HUE_CHROMA)
        & between
    )
    surface = np.ones(grey.shape, dtype=bool)
    if matte is not None:
        surface = np.asarray(matte, dtype=np.float32)[window][band] > KEY_CLEAR_MATTE
    whole = round(float(grey.mean()), 4)
    surroundings = _rim_part(grey, ~surface) if matte is not None else None
    if footage is not None:
        # A pixel the footage itself shows grey was never the key's: no despill made it so.
        before = screens.to_ycc(np.asarray(footage, dtype=np.float32)[window][band])
        grey = grey & (np.hypot(before[:, 1], before[:, 2]) >= screens.MIN_HUE_CHROMA)
    rim = grey & surface
    share = _rim_part(rim, surface) if matte is not None else round(float(rim.mean()), 4)
    # Off the keyed surface, only the grey the compositing made: the footage's own grey was filtered out above.
    off = _rim_part(grey, ~surface) if matte is not None and footage is not None else None
    if off is not None and off > MAX_RIM_SHARE:
        rim = rim | (grey & ~surface)
    if (share is None and off is None) or not rim.any():
        return RimRead(share, clear.edge, surroundings=surroundings, whole=whole, off=off)
    greyed = (np.hypot(lit_ycc[:, 1], lit_ycc[:, 2]) < screens.MIN_HUE_CHROMA) & (np.asarray(drawn, dtype=np.float32)[window][band] >= 0.5)
    hits = np.zeros(np.asarray(judged).shape, dtype=bool)
    placed = np.zeros(band.shape, dtype=bool)
    placed[band] = rim
    hits[window] = placed
    return RimRead(
        share,
        clear.edge,
        round(float((rim & greyed).sum()) / float(rim.sum()), 4),
        hits,
        surroundings=surroundings,
        whole=whole,
        off=off,
    )


def _laplacian_variance(grey: Any, mask: Any) -> float | None:
    """The variance of the Laplacian of *grey* over *mask* (eroded, so no edge of the mask counts); ``None`` for too few."""
    import numpy as np
    from scipy import ndimage

    judged = ndimage.binary_erosion(mask, iterations=2)
    if int(judged.sum()) < 400:
        return None
    return float(ndimage.laplace(np.asarray(grey, dtype=np.float32))[judged].var())


def _sharpness(
    composed_luma: Any,
    showing: Any,
    picture: Any,
    fit: str,
    quad_px: Any,
    state: Matting | None = None,
) -> float | None:
    """How sharp the insert is drawn: its Laplacian variance over the source's, fitted to the quad's own size.

    Read on *showing* - the source drawn opaque, clear of what stands in front and of the contain bands
    (:meth:`GateMasks.clean`). The source is shaped as the insert draws it (``pictures.fitted``) and
    brought to the quad's mean width and height (LANCZOS): what a sharp
    drawing of it at the output size holds - read once per picture and quad
    size when a *state* keeps it (a still source is the same on every frame).
    ``None`` with too little drawn, or a flat source with nothing to lose.
    """
    import numpy as np
    from PIL import Image

    drawn = _laplacian_variance(composed_luma, np.asarray(showing, dtype=bool))
    if drawn is None:
        return None
    quad_w, quad_h = quad_size(tuple(map(tuple, quad_px)))
    if quad_w < 8 or quad_h < 8:
        return None
    reference_size = (max(1, round(quad_w)), max(1, round(quad_h)))
    cached = (id(picture), picture.size, fit, reference_size)
    own = state.reference.get(cached) if state is not None else None
    if own is None:
        shaped = fitted(picture.convert("RGBA"), quad_w, quad_h, fit, pad=LETTERBOX)
        pixels = np.asarray(shaped.resize(reference_size, Image.Resampling.LANCZOS), dtype=np.float32)
        grey = pixels[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        own = _laplacian_variance(grey, pixels[..., 3] >= 128) or 0.0
        if state is not None:
            state.reference = {cached: own}
    if own < 1.0:
        return None
    return round(min(drawn / own, 4.0), 4)


def _hsv(rgb: Any) -> Any:
    """*rgb* (``H x W x 3`` float) as HSV, 0-1 each: read once a frame for the hue gates."""
    import numpy as np
    from PIL import Image

    return np.asarray(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("HSV"), dtype=np.float32) / 255.0


def _hue_outliers(hsv: Any, key: Any, *, complement: bool, saturation: float = HALO_MIN_SATURATION) -> Any:
    """The pixels of *hsv* (:func:`_hsv`) of the key's hue (or of its complement), saturated past *saturation*."""
    import colorsys

    import numpy as np

    hue = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in key.rgb))[0] * 360.0
    if complement:
        hue = (hue + 180.0) % 360.0
    apart = np.abs((hsv[..., 0] * 360.0 - hue + 180.0) % 360.0 - 180.0)
    return (apart <= HALO_HUE_DEGREES) & (hsv[..., 1] >= saturation) & (hsv[..., 2] >= 0.15)


def _residual_key(hsv: Any, key: Any, near: Any, quad_pixels: int) -> tuple[float, Any]:
    """The share of the quad still showing the key after compositing, and those pixels (``None``: none):
    connected regions of key-hue pixels inside the surface or within :data:`KEY_MARGIN` of it (*near*), each at least
    :data:`RESIDUAL_MIN_SHARE` of it. *hsv* is the composed frame's (:func:`_hsv`)."""
    import numpy as np
    from scipy import ndimage

    keyed = _hue_outliers(hsv, key, complement=False, saturation=RESIDUAL_MIN_SATURATION)
    if near is not None:
        keyed &= np.asarray(near, dtype=bool)
    labels, count = ndimage.label(keyed)
    if count == 0:
        return 0.0, None
    sizes = ndimage.sum(keyed, labels, range(1, count + 1))
    smallest = max(RESIDUAL_MIN_PIXELS, RESIDUAL_MIN_SHARE * quad_pixels)
    counted = [index + 1 for index, size in enumerate(sizes) if size >= smallest]
    if not counted:
        return 0.0, None
    left = np.isin(labels, counted)
    return round(float(left.sum()) / max(quad_pixels, 1), 5), left


def _residual_where(
    left: Any, inside: Any, edge: Any, quad_px: Any, quad_pixels: int, contain: Any = None
) -> dict[str, dict[str, float]]:
    """Where the residual key *left* sits (boolean over the box): ``{region: {side: share of the quad}}``.

    The region is ``inside`` the drawn quad (*inside*) where the source is drawn, ``contain`` - in the bands a contain
    fit leaves inside it (*contain*) -, ``edge`` - the surface running past it, joined to it (``Band.edge``,
    *edge*, two pixels around) - or ``band``: the footage's key colour past the bezel the key did not take; the side is
    the quad's edge nearest (:data:`SIDES`, *quad_px* in the box's pixels).
    """
    import numpy as np
    from scipy import ndimage

    ys, xs = np.nonzero(left)
    if not len(ys):
        return {}
    joined = ndimage.binary_dilation(edge, iterations=2) if edge is not None and edge.any() else None
    banded = contain[ys, xs] if contain is not None else np.zeros(len(ys), dtype=bool)
    outside = np.where(joined[ys, xs], 2, 3) if joined is not None else 3
    regions = np.where(inside[ys, xs], np.where(banded, 1, 0), outside)
    sides = _sides(np.stack([xs + 0.5, ys + 0.5], axis=1), quad_px)
    where: dict[str, dict[str, float]] = {}
    for region, name in enumerate(RESIDUAL_REGIONS):
        for side, label in enumerate(SIDES):
            count = int(((regions == region) & (sides == side)).sum())
            if count:
                where.setdefault(name, {})[label] = round(count / max(quad_pixels, 1), 5)
    return where


def _side_shares(mask: Any, quad_px: Any, quad_pixels: int) -> dict[str, float]:
    """The pixels of *mask* (boolean over the box) by the side of *quad_px* nearest each, a share of the quad each."""
    import numpy as np

    ys, xs = np.nonzero(mask)
    if not len(ys):
        return {}
    sides = _sides(np.stack([xs + 0.5, ys + 0.5], axis=1), quad_px)
    counts = np.bincount(sides, minlength=len(SIDES))
    return {SIDES[s]: round(int(counts[s]) / max(quad_pixels, 1), 5) for s in range(len(SIDES)) if counts[s]}


def _hue_saturation(pixels: Any) -> tuple[Any, Any, Any]:
    """HSV hue (degrees), saturation (0-1) and value (0-255) of *pixels* (``N x 3`` float RGB 0-255)."""
    import numpy as np

    top, low = pixels.max(axis=1), pixels.min(axis=1)
    delta = top - low
    safe = np.maximum(delta, 1e-6)
    r, g, b = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    hue = np.where(top == r, ((g - b) / safe) % 6.0, np.where(top == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0)) * 60.0
    saturation = np.where(top > 0, delta / np.maximum(top, 1e-6), 0.0)
    return np.where(delta > 1e-6, hue, 0.0), saturation, top


def _key_hue_of(pixels: Any, key: Any) -> tuple[Any, Any]:
    """Which of *pixels* (``N x 3``) are of *key*'s hue (:data:`HALO_HUE_DEGREES`, never too dark), and their saturation."""
    import colorsys

    import numpy as np

    hue, saturation, value = _hue_saturation(pixels)
    key_hue = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in key.rgb))[0] * 360.0
    apart = np.abs((hue - key_hue + 180.0) % 360.0 - 180.0)
    return (apart <= HALO_HUE_DEGREES) & (value >= 0.15 * 255.0), saturation


def _key_hued(rgb: Any, key: Any, where: Any) -> Any:
    """The pixels of *rgb* (``H x W x 3``) within *where* of *key*'s hue saturated past :data:`DESPILL_MIN_SATURATION`."""
    import numpy as np

    zone = np.asarray(where, dtype=bool)
    out = np.zeros(zone.shape, dtype=bool)
    if not zone.any():
        return out
    ys, xs = np.nonzero(zone)
    hued, saturation = _key_hue_of(np.asarray(rgb, dtype=np.float32)[ys, xs], key)
    keyed = hued & (saturation >= DESPILL_MIN_SATURATION)
    out[ys[keyed], xs[keyed]] = True
    return out


def _despill_margin(rgb: Any, key: Any, where: Any, scale: float) -> Any:
    """*rgb* (``H x W x 3`` float, the layer over its box) with the key's colour taken out where *where* says.

    Each pixel there of the key's hue (:data:`HALO_HUE_DEGREES`) saturated
    past :data:`DESPILL_MIN_SATURATION` is brought toward the luminance of
    the pixels of another hue around it (a Gaussian of :data:`DESPILL_SIGMA`
    pixels of a 1080p frame; its own when none is near) - fully from
    :data:`RESIDUAL_MIN_SATURATION` -: a green glow on a dark bezel goes the
    bezel's grey, never black, never a bright line.
    """
    import numpy as np
    from scipy import ndimage

    zone = np.asarray(where, dtype=bool)
    if not zone.any():
        return rgb
    ys, xs = np.nonzero(zone)
    pixels = np.asarray(rgb, dtype=np.float32)[ys, xs]
    hued, saturation = _key_hue_of(pixels, key)
    keyed = hued & (saturation >= DESPILL_MIN_SATURATION)
    if not keyed.any():
        return rgb
    luma = np.asarray(rgb, dtype=np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    other = np.zeros(zone.shape, dtype=np.float32)
    other[ys[~keyed], xs[~keyed]] = 1.0
    sigma = max(1.0, DESPILL_SIGMA * scale)
    weight = ndimage.gaussian_filter(other, sigma)
    total = ndimage.gaussian_filter(other * luma, sigma)
    ky, kx = ys[keyed], xs[keyed]
    around = weight[ky, kx]
    local = np.where(around > 1e-3, total[ky, kx] / np.maximum(around, 1e-3), luma[ky, kx])
    strength = np.clip((saturation[keyed] - DESPILL_MIN_SATURATION) / (RESIDUAL_MIN_SATURATION - DESPILL_MIN_SATURATION), 0.0, 1.0)
    out = np.array(rgb, dtype=np.float32, copy=True)
    out[ky, kx] = pixels[keyed] * (1.0 - strength[:, None]) + local[:, None] * strength[:, None]
    return out


def worst_side(where: dict[str, dict[str, float]] | None) -> str | None:
    """The side of the quad holding the most of a residual key (``FrameAudit.residual_where``), whatever its region."""
    totals: dict[str, float] = {}
    for sides in (where or {}).values():
        for side, share in sides.items():
            totals[side] = totals.get(side, 0.0) + float(share)
    return max(totals, key=lambda side: totals[side]) if totals else None


def _residual_crop(composed: Any, where: dict[str, dict[str, float]], quad_px: Any, margin: float) -> Any:
    """The composed pixels (``H x W x 3`` over the box) along the side holding the most residual key, unmarked, as an
    RGB picture: that edge of *quad_px* (the box's pixels) and *margin* either side of it - the band past it and as deep
    inside it - at 100 %; ``None`` when nothing is left of it on the box."""
    import numpy as np
    from PIL import Image

    side = worst_side(where)
    if side is None:
        return None
    index = SIDES.index(side)
    corners = np.asarray(quad_px, dtype=float)
    ends = np.stack([corners[index], corners[(index + 1) % len(SIDES)]])
    height, width = composed.shape[:2]
    reach = margin + 2.0
    x0, x1 = max(0, math.floor(ends[:, 0].min() - reach)), min(width, math.ceil(ends[:, 0].max() + reach))
    y0, y1 = max(0, math.floor(ends[:, 1].min() - reach)), min(height, math.ceil(ends[:, 1].max() + reach))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return Image.fromarray(np.clip(composed[y0:y1, x0:x1], 0, 255).astype(np.uint8), "RGB")


@dataclass(frozen=True)
class HaloRead:
    """What the halo gate read on one frame (:func:`_halo_share`); ``None`` for what it did not read."""

    #: The hue outliers counted along the foreground's edge, a share of that edge once the subject's own colours are
    #: left out of it ...
    share: float | None = None
    #: ... the share of that edge left out as the subject's own colour (``None``: no :class:`Edge`) ...
    own: float | None = None
    #: ... the edge's length in pixels ...
    edge: int | None = None
    #: ... the share of it counted on pixels the compositor's clamp touched (``None``: no :class:`Edge`) ...
    clamped: float | None = None
    #: ... and the mean hue of what was counted, in degrees (``None``: nothing counted).
    hue: float | None = None


def _halo_share(hsv: Any, foreground: Any, inside: Any, key: Any, scale: float, *, own: Edge | None = None, source: Any = None) -> HaloRead:
    """Hue outliers along the foreground's edge, a share of that edge: the key's green left on a person's
    fringe, the magenta an inverted despill leaves; nothing read without a foreground. *hsv* is the composed frame's
    (:func:`_hsv`).

    With *own* (:func:`edge_of`), an edge pixel of the subject's own colour (:meth:`Edge.owned`) is left
    out of the edge and said, never blamed on the insert - a pink muzzle, a green-lit person -, and the share counted
    where the compositor's clamp touched is said apart; without it, a foreground of that hue itself is not read. With
    *source* (:class:`GateMasks`), the edge over the source's own key hue or its complement is left out too - a
    hand crossing a product UI's green ring.
    """
    import numpy as np
    from scipy import ndimage

    if foreground is None or not foreground.any():
        return HaloRead()
    band, boundary, reach = _halo_band(foreground, inside, scale, source)
    length = int(boundary.sum())
    if int(band.sum()) < 64:
        return HaloRead(edge=length)
    keyed, opposite = _hue_outliers(hsv, key, complement=False), _hue_outliers(hsv, key, complement=True)
    outliers = keyed | opposite
    if own is None:
        interior = ndimage.binary_erosion(foreground, iterations=2 * reach) & inside
        if int(interior.sum()) >= 64 and float(outliers[interior].mean()) > 0.3:
            return HaloRead(edge=length)
        return HaloRead(round(float(outliers[band].mean()), 4), edge=length, hue=_mean_hue(hsv, outliers & band))
    mine = band & outliers & own.owned(hsv, keyed, opposite, key)
    left_out = round(float(mine.sum()) / float(band.sum()), 4)
    band = band & ~mine
    if int(band.sum()) < 64:
        return HaloRead(own=left_out, edge=length)
    counted = outliers & band
    touched = counted & own.clamped if own.clamped is not None else np.zeros(band.shape, dtype=bool)
    total = float(band.sum())
    return HaloRead(
        round(float(counted.sum()) / total, 4), left_out, length, round(float(touched.sum()) / total, 4), _mean_hue(hsv, counted)
    )


def _mean_hue(hsv: Any, where: Any) -> float | None:
    """The mean hue of *hsv*'s pixels *where* says, in degrees (around the wheel); ``None`` for none."""
    import numpy as np

    if not where.any():
        return None
    angle = hsv[..., 0][where].astype(np.float64) * 2.0 * math.pi
    return round(math.degrees(math.atan2(float(np.sin(angle).mean()), float(np.cos(angle).mean()))) % 360.0, 1)


def _halo_band(foreground: Any, inside: Any, scale: float, source: Any = None) -> tuple[Any, Any, int]:
    """The band the halo is read on: ``(band, boundary, reach)`` - *reach* pixels either side of the
    *foreground*'s boundary inside the quad, and that boundary, both clear of the source's own key hue (*source*)."""
    from scipy import ndimage

    reach = max(2, math.ceil(2.5 * scale))
    band = (ndimage.binary_dilation(foreground, iterations=reach) & ~ndimage.binary_erosion(foreground, iterations=reach)) & inside
    boundary = foreground & ~ndimage.binary_erosion(foreground, iterations=1) & inside
    if source is not None:
        band &= ~source
        boundary &= ~source
    return band, boundary, reach


def _halo_crop(
    composed: Any, hsv: Any, foreground: Any, inside: Any, key: Any, scale: float, *, own: Edge | None = None, source: Any = None
) -> Any:
    """The worst stretch of a halo as the frame shows it: the composed pixels (``H x W x 3`` over the box) around
    the largest cluster of the hue outliers :func:`_halo_share` counts, :data:`HALO_CROP_MARGIN_PX` of a 1080p frame
    around it and at least :data:`HALO_CROP_MIN_PX` a side, unmarked, at 100 %; ``None`` with none."""
    if foreground is None or not foreground.any():
        return None
    band, _boundary, reach = _halo_band(foreground, inside, scale, source)
    keyed, opposite = _hue_outliers(hsv, key, complement=False), _hue_outliers(hsv, key, complement=True)
    hits = band & (keyed | opposite)
    if own is not None:
        hits &= ~own.owned(hsv, keyed, opposite, key)
    if not hits.any():
        return None
    return _crop_around(composed, hits, scale, 2 * reach)


def _front_crop(composed: Any, over: Any, scale: float) -> Any:
    """The worst stretch of the insert drawn over what stands in front: the composed pixels (``H x W x 3`` over
    the box) around the largest cluster of *over* (``FrameAudit.covered``'s pixels), unmarked, at 100 %; ``None``
    with none."""
    if over is None or not over.any():
        return None
    return _crop_around(composed, over, scale, max(1, math.ceil(2.0 * scale)))


def through_share(composed: Any, region: Any, shown: Any, judged: Any, scale: float) -> tuple[float | None, Any]:
    """How much of *judged* - the core of what stands in front (boolean over the box) - shows the source's own strokes
    drawn across it, and those pixels (``None``: none).

    The high frequencies the drawing added there - *composed* less the
    footage *region*, its luma less its Gaussian of :data:`THROUGH_SIGMA_PX`
    pixels of a 1080p frame (*scale*: the frame's height over 1080), read on
    *judged* alone - are held to the source's own at that place (*shown*, the
    source warped into the quad) in tiles :data:`THROUGH_TILE_PX` a side, a
    tile read where :data:`THROUGH_TILE_SHARE` of it lies in *judged*: one
    whose source holds an edge (:data:`THROUGH_MIN_SOURCE`), whose added
    detail a viewer sees (:data:`THROUGH_MIN_ADDED`) and whose two correlate
    past :data:`THROUGH_MIN_CORRELATION` shows the source through what stands
    in front - a row separator drawn as a dash across a subject. What the
    footage itself holds there is never counted: nothing drawn adds nothing.
    """
    import numpy as np
    from scipy import ndimage

    mask = np.asarray(judged, dtype=bool)
    count = int(mask.sum())
    if count == 0:
        return None, None
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    sigma = max(0.5, THROUGH_SIGMA_PX * scale)
    added = (np.asarray(composed, dtype=np.float32) - np.asarray(region, dtype=np.float32)) @ weights * mask
    source = np.asarray(shown, dtype=np.float32) @ weights
    added = added - ndimage.gaussian_filter(added, sigma)
    source = source - ndimage.gaussian_filter(source, sigma)
    tile = max(4, round(THROUGH_TILE_PX * scale))
    height, width = mask.shape
    rows, cols = math.ceil(height / tile), math.ceil(width / tile)

    def tiled(values: Any) -> Any:
        padded = np.zeros((rows * tile, cols * tile), dtype=np.float64)
        padded[:height, :width] = values
        return padded.reshape(rows, tile, cols, tile).sum(axis=(1, 3))

    weight = mask.astype(np.float64)
    n = tiled(weight)
    a, b = added * weight, source * weight
    sum_a, sum_b, sum_aa, sum_bb, sum_ab = tiled(a), tiled(b), tiled(a * a), tiled(b * b), tiled(a * b)
    read = n >= THROUGH_TILE_SHARE * tile * tile
    safe = np.maximum(n, 1.0)
    mean_a, mean_b = sum_a / safe, sum_b / safe
    var_a = np.maximum(sum_aa / safe - mean_a**2, 0.0)
    var_b = np.maximum(sum_bb / safe - mean_b**2, 0.0)
    correlation = (sum_ab / safe - mean_a * mean_b) / np.sqrt(np.maximum(var_a * var_b, 1e-9))
    seen = np.sqrt(sum_aa / safe) >= THROUGH_MIN_ADDED
    edged = np.sqrt(var_b) >= THROUGH_MIN_SOURCE
    through = read & seen & edged & (correlation > THROUGH_MIN_CORRELATION)
    if not through.any():
        return 0.0, None
    hits = np.repeat(np.repeat(through, tile, axis=0), tile, axis=1)[:height, :width] & mask
    return round(float(hits.sum()) / count, 5), hits


@dataclass(frozen=True)
class MatteRead:
    """What an unkeyed insert's mask of what stands in front holds past the subject on one frame."""

    #: The share of the mask's edge with the surface left showing beside it inside the mask, and the largest connected
    #: region of it, a share of the whole frame.
    halo: float | None = None
    halo_region: float | None = None
    #: The mask's centroid against the subject's read from the footage, in pixels of a 1080p frame.
    lag: float | None = None
    #: The surface left showing (boolean over the box), for the 100 % crop.
    hits: Any = None


def matte_read(region: Any, alpha: Any, foreground: Any, inside: Any, scale: float, frame_pixels: int) -> MatteRead:
    """What an unkeyed insert's mask *foreground* (boolean over the box) holds that is no subject.

    The surface is the footage *region* where the insert is drawn, clear of
    the mask: its median colour, read only when it is flat (its pixels within
    :data:`MATTE_HALO_TOLERANCE` of it at the 90th percentile) - a dark
    screen, never a textured one, where no colour tells the subject apart. A
    pixel of the band :data:`MATTE_HALO_BAND_PX` deep inside the mask's edge
    (scaled, at least :data:`MATTE_HALO_MIN_PX`) where the insert is not drawn
    (*alpha* under a half) and the footage is that surface is the surface
    left showing: the halo. The subject is the footage inside the quad away
    from that surface; its centroid against the mask's is the lag.
    """
    import numpy as np
    from scipy import ndimage

    mask = np.asarray(foreground, dtype=bool) & inside
    clear = inside & ~ndimage.binary_dilation(mask, iterations=max(1, math.ceil(MATTE_HALO_BAND_PX * scale)))
    if not mask.any() or int(clear.sum()) < 64:
        return MatteRead()
    pixels = np.asarray(region, dtype=np.float32)
    surface = np.median(pixels[clear], axis=0)
    apart = np.abs(pixels - surface).max(axis=-1)
    if float(np.percentile(apart[clear], 90)) > MATTE_HALO_TOLERANCE:
        return MatteRead()
    flat = apart <= MATTE_HALO_TOLERANCE
    depth = max(MATTE_HALO_MIN_PX, math.ceil(MATTE_HALO_BAND_PX * scale))
    band = mask & ~ndimage.binary_erosion(mask, iterations=depth)
    hits = band & flat & (np.asarray(alpha) < 0.5)
    # The gaps between a subject's strokes the mask is closed over (:func:`close_width`) are no halo: only the surface
    # wider than that kernel is.
    hits = ndimage.binary_opening(hits, iterations=close_width(scale) // 2 + 1) if hits.any() else hits
    boundary = mask & ~ndimage.binary_erosion(mask, iterations=1)
    length = int(boundary.sum())
    out_halo = out_region = None
    if length:
        near = ndimage.binary_dilation(hits, iterations=depth) & boundary if hits.any() else np.zeros_like(boundary)
        out_halo = round(float(near.sum()) / length, 4)
        if hits.any():
            labels, count = ndimage.label(hits)
            largest = float(np.max(ndimage.sum(hits, labels, range(1, count + 1))))
            out_region = round(largest / max(1, frame_pixels), 6)
        else:
            out_region = 0.0
    # The subject: what is no surface near the mask, the mask's own reach and as far again - never another thing in the
    # quad.
    subject = inside & ~flat & ndimage.binary_dilation(mask, iterations=2 * depth)
    lag = None
    # A subject the footage cannot tell from the surface over most of its mask (dark on a dark screen) has no centroid
    # to read: no lag, rather than the centroid of the rim and the grain around it.
    if subject.any() and float((subject & mask).sum()) >= MATTE_LAG_TOLD_SHARE * float(mask.sum()):
        # A mask that lags its subject leaves the subject's leading edge uncovered and the insert drawn over it; a mask
        # that covers the whole subject and more is the surface left showing (the halo above), never a lag.
        uncovered = subject & ~ndimage.binary_dilation(mask, iterations=1) & (np.asarray(alpha) >= 0.5)
        uncovered = ndimage.binary_opening(uncovered, iterations=1) if uncovered.any() else uncovered
        if float(uncovered.sum()) < MATTE_LAG_UNCOVERED_SHARE * float(subject.sum()):
            lag = 0.0
        else:
            ys, xs = np.nonzero(mask)
            sy, sx = np.nonzero(subject)
            lag = round(math.hypot(float(xs.mean() - sx.mean()), float(ys.mean() - sy.mean())) / max(scale, 1e-6), 2)
    return MatteRead(out_halo, out_region, lag, hits if hits.any() else None)


@dataclass(frozen=True)
class Spill:
    """The key's hue past a keyed insert's quad on one footage frame (:func:`key_spill`)."""

    #: The share of the frame its regions past :data:`SPILL_MIN_SHARE` hold, and the largest one's box, 0-1 of the
    #: frame (``None``: none).
    share: float | None = None
    box: list[float] | None = None
    #: Those regions on the frame read every *step* pixels (boolean; ``None``: none).
    hits: Any = None
    step: int = 1
    #: The largest region joined to the layer's margin around the quad - the key running on from the quad's edge, the
    #: screen continuing beside or behind what stands in front -: the quad's side it runs from (``top``, ``right``,
    #: ``bottom``, ``left``) and how far it runs, x or y 0-1 of the frame; ``None`` each: detached (a wall, a floor).
    joined: str | None = None
    reaches: float | None = None

    def picture(self, footage: Any, composed: Any, box: tuple[int, int, int, int]) -> Any:
        """The whole *footage* frame, the layer as composited over *box* (*composed*) drawn on it and these regions
        marked: what a ``key_spill_outside_quad`` finding shows. ``None`` with none."""
        import numpy as np
        from PIL import Image

        if self.hits is None:
            return None
        frame = np.array(footage[..., :3], dtype=np.float32, copy=True)
        x0, y0, x1, y1 = box
        frame[y0:y1, x0:x1] = composed
        height, width = frame.shape[:2]
        marked = np.repeat(np.repeat(self.hits, self.step, axis=0), self.step, axis=1)[:height, :width]
        frame[marked] = frame[marked] * 0.4 + np.array([255.0, 0.0, 0.0]) * 0.6
        return Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8), "RGB")


def key_spill(
    footage: Any,
    key: Any,
    quad_px: Any,
    others: Any = (),
    *,
    subject: Subject | None = None,
    declared: tuple[tuple[float, float], ...] = (),
) -> Spill:
    """The key's hue past a keyed insert's quad on its whole *footage* frame (``H x W x 3`` at the host's size):
    the connected regions of *key*'s hue (:func:`_key_hued`) outside *quad_px* (host pixels) grown by the layer's margin
    (:data:`KEY_MARGIN` of the frame's width) - and outside the quads *others* (0-1 corners) grown the same, the other
    surfaces its scene draws on the footage -, each larger than :data:`SPILL_MIN_SHARE` of the frame, the subject's
    own hues (*subject*, :class:`Subject`) and those its author declares (*declared*, hue spans) left out. Read on the
    frame every few pixels (:data:`SPILL_ROWS` rows): a glossy floor reflecting a green panel, a wall it lights - what
    no margin of the layer reaches. Inside the quad the key's hue is the source's own or the residual key's."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy import ndimage

    pixels = np.asarray(footage)
    height, width = pixels.shape[:2]
    step = max(1, round(height / SPILL_ROWS))
    small = pixels[::step, ::step, :3].astype(np.float32)
    rows, cols = small.shape[:2]
    surfaces = Image.new("L", (cols, rows), 0)
    draw = ImageDraw.Draw(surfaces)
    for corners in (np.asarray(quad_px, dtype=float), *(np.array([(x * width, y * height) for x, y in other], dtype=float) for other in others)):
        draw.polygon([(float(x) / step, float(y) / step) for x, y in corners], fill=255)
    reach = (KEY_MARGIN * width + 2.0) / step
    outside = ndimage.distance_transform_edt(np.asarray(surfaces) == 0) > reach
    hued = _key_hued(small, key, outside)
    if hued.any() and (subject is not None or declared):
        ys, xs = np.nonzero(hued)
        degrees = _hue_saturation(small[ys, xs])[0]
        owned = in_spans(degrees, declared)
        if subject is not None:
            owned |= subject.owns(degrees)
        hued[ys[owned], xs[owned]] = False
    labels, count = ndimage.label(hued)
    if count == 0:
        return Spill(step=step)
    sizes = ndimage.sum(hued, labels, range(1, count + 1))
    counted = [index + 1 for index, size in enumerate(sizes) if size > SPILL_MIN_SHARE * rows * cols]
    if not counted:
        return Spill(step=step)
    hits = np.isin(labels, counted)
    largest = labels == counted[int(np.argmax([sizes[index - 1] for index in counted]))]
    ys, xs = np.nonzero(largest)
    box = [
        round(float(xs.min()) * step / width, 4),
        round(float(ys.min()) * step / height, 4),
        round(min(width, float(xs.max() + 1) * step) / width, 4),
        round(min(height, float(ys.max() + 1) * step) / height, 4),
    ]
    own = Image.new("L", (cols, rows), 0)
    ImageDraw.Draw(own).polygon(
        [(float(x) / step, float(y) / step) for x, y in np.asarray(quad_px, dtype=float)], fill=255
    )
    inside = np.asarray(own) > 0
    margin = ~inside & (ndimage.distance_transform_edt(~inside) <= reach + 1.5)
    joined, reaches = _joined_side(
        largest, _key_hued(small, key, margin), inside, np.asarray(quad_px, dtype=float) / step, box
    )
    return Spill(round(float(hits.sum()) / (rows * cols), 5), box, hits, step, joined, reaches)


def _joined_side(
    region: Any, keyed_margin: Any, inside: Any, quad: Any, box: list[float]
) -> tuple[str | None, float | None]:
    """The side of *quad* (corners, in the pixels of *region*) a key region (boolean) runs on from - joined to the
    quad (*inside*, boolean) by the key's hue across the layer's margin (*keyed_margin*, boolean) -, and how far it
    runs (from *box*, 0-1 of the frame); ``(None, None)`` for a region parted from the quad: a detached spill on a wall
    or a floor - or for one running past ``surface_refine.HIDDEN_REACH`` of the quad's span across that side: a
    key-coloured wall, which the save never widens the quad onto either."""
    import numpy as np
    from scipy import ndimage

    from app.services.video_studio.surface_refine import HIDDEN_REACH

    region = np.asarray(region, dtype=bool)
    inside = np.asarray(inside, dtype=bool)
    labels, _count = ndimage.label(region | np.asarray(keyed_margin, dtype=bool))
    own = np.unique(labels[region])
    own = own[own > 0]
    path = np.isin(labels, own) & ~region
    touching = path & ndimage.binary_dilation(inside, iterations=1)
    if not touching.any():
        return None, None
    ys, xs = np.nonzero(touching)
    point = np.array([float(xs.mean()), float(ys.mean())])
    corners = np.asarray(quad, dtype=float).reshape(4, 2)
    centre = corners.mean(axis=0)
    best, side, chosen = 0.0, None, -1
    for index, name in enumerate(("top", "right", "bottom", "left")):
        start, end = corners[index], corners[(index + 1) % 4]
        along = end - start
        length = float(np.linalg.norm(along))
        if length < 1e-6:
            continue
        normal = np.array([along[1], -along[0]]) / length
        if float(np.dot(normal, (start + end) / 2.0 - centre)) < 0.0:
            normal = -normal
        past = float(np.dot(point - start, normal))
        if past > best:
            best, side, chosen = past, name, index
    if side is None:
        return None, None
    # How far the region runs past that side, against the quad's span across it: the bound the save keeps
    # (``surface_refine.hidden_sides``), so the gate never advises a widening the save would refuse.
    start, end = corners[chosen], corners[(chosen + 1) % 4]
    along = end - start
    normal = np.array([along[1], -along[0]]) / float(np.linalg.norm(along))
    if float(np.dot(normal, (start + end) / 2.0 - centre)) < 0.0:
        normal = -normal
    opposite = (corners[(chosen + 2) % 4] + corners[(chosen + 3) % 4]) / 2.0
    span = float(np.linalg.norm((start + end) / 2.0 - opposite))
    ys, xs = np.nonzero(region)
    run = float(np.max((np.stack([xs, ys], axis=1).astype(float) + 0.5 - start) @ normal))
    if run > HIDDEN_REACH * span:
        return None, None
    reaches = {"top": box[1], "right": box[2], "bottom": box[3], "left": box[0]}[side]
    return side, reaches


def _crop_around(composed: Any, hits: Any, scale: float, join: int) -> Any:
    """*composed* (``H x W x 3`` over the box) around the largest cluster of *hits* - those within *join* pixels of each
    other one -, :data:`HALO_CROP_MARGIN_PX` of a 1080p frame around it and at least :data:`HALO_CROP_MIN_PX` a side."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    labels, count = ndimage.label(ndimage.binary_dilation(hits, iterations=join))
    sizes = ndimage.sum(hits, labels, range(1, count + 1))
    ys, xs = np.nonzero(hits & (labels == int(np.argmax(sizes)) + 1))
    height, width = hits.shape
    margin = math.ceil(HALO_CROP_MARGIN_PX * scale)

    def span(low: int, high: int, size: int) -> tuple[int, int]:
        low, high = max(0, low - margin), min(size, high + margin + 1)
        short = HALO_CROP_MIN_PX - (high - low)
        if short > 0:
            low, high = max(0, low - short // 2 - short % 2), min(size, high + short // 2)
        return low, high

    y0, y1 = span(int(ys.min()), int(ys.max()), height)
    x0, x1 = span(int(xs.min()), int(xs.max()), width)
    return Image.fromarray(np.clip(composed[y0:y1, x0:x1], 0, 255).astype(np.uint8), "RGB")


def draw(insert: InsertSource, picture: Any, size: tuple[int, int], footage_frame: int | None = None) -> tuple[bytes, bytes | None]:
    """The PNG of *picture* (the insert's source frame) mapped onto its quad at *size*, and its glow's when it glows.

    Unmatted: the frame server draws through :func:`compose` with the footage.
    """
    from PIL import Image

    layer = compose(insert, picture, size, footage_frame)
    if layer is None:
        empty = png(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        return empty, empty if insert.glow > 0 else None
    return layer.pngs()


def padded_sides(picture: Any, fit: str, quad_w: float, quad_h: float) -> str | None:
    """Where *picture* - drawn into a quad of that size with a ``contain`` fit - shows a letterbox baked into it.

    ``top and bottom`` or ``left and right`` (a black or a white band each
    side of what the picture shows), ``None`` when it has none. Only a contain
    fit is judged: a ``cover`` one fills the quad, so its crop is what
    the surface shows (*quad_w* and *quad_h* are kept for the caller's
    reading). Only the picture's own pixels count: its alpha-zero rows
    and columns are cut away first - a true-alpha PNG's transparent
    margin shows the surface, never a band of the insert's - and a band line
    is at least nine tenths :data:`OPAQUE_ALPHA` opaque, its colour, read on
    those opaque pixels alone, flat: a glow fading into the surface is never
    one. And a band is padding only when its colour differs from the
    picture's own border on the other two sides (:func:`_same_colour`):
    a brushed plate whose top band is its sides' colour is one surface with a
    mark on it, never a card.
    """
    import numpy as np

    del quad_w, quad_h
    if fit != "contain":
        return None
    source = picture.convert("RGBA")
    shown = np.asarray(source)[..., 3] > 0
    if not shown.any():
        return None
    ys, xs = np.nonzero(shown)
    small = source.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    small.thumbnail((256, 256))
    pixels = np.asarray(small, dtype=np.float32)
    if pixels.shape[0] < 8 or pixels.shape[1] < 8:
        return None
    rgb, opaque = pixels[..., :3], pixels[..., 3] / 255.0 >= OPAQUE_ALPHA

    def flat_lines(axis: int) -> Any:
        weight = opaque[..., None].astype(np.float32)
        count = np.maximum(weight.sum(axis=axis), 1.0)
        mean = np.expand_dims((rgb * weight).sum(axis=axis) / count, axis)
        spread = np.sqrt((((rgb - mean) ** 2) * weight).sum(axis=axis) / count).max(axis=-1)
        return (opaque.mean(axis=axis) >= 0.9) & (spread < FLAT_SPREAD)

    def run(flags: Any) -> int:
        count = 0
        for flag in flags:
            if not flag:
                break
            count += 1
        return count

    def colour(lines: Any) -> Any:
        """The median opaque colour of *lines* (an index of ``rgb``); ``None`` for none."""
        chosen = rgb[lines][opaque[lines]]
        return np.median(chosen, axis=0) if len(chosen) else None

    def letterbox(first: int, last: int, along_rows: bool) -> bool:
        """Whether the bands ``[0, first)`` and ``[last, end)`` (rows, or columns) differ from the border between them:
        the first and the last column of those rows (or row of those columns)."""
        if last <= first:
            return False
        if along_rows:
            bands, sides = (np.s_[:first], np.s_[last:]), np.s_[first:last, :: max(1, rgb.shape[1] - 1)]
        else:
            bands, sides = (np.s_[:, :first], np.s_[:, last:]), np.s_[:: max(1, rgb.shape[0] - 1), first:last]
        border = colour(sides)
        return all(not _same_colour(colour(band), border) for band in bands)

    rows, columns = flat_lines(1), flat_lines(0)
    # A picture of one colour all over is a flat picture, never a padded one.
    if rows.all() or columns.all():
        return None
    height, width = opaque.shape
    top, bottom = run(rows[1:]) + 1, run(rows[::-1][1:]) + 1
    if min(top, bottom) - 1 >= PAD_MIN_SHARE * height and letterbox(top, height - bottom, True):
        return "top and bottom"
    left, right = run(columns[1:]) + 1, run(columns[::-1][1:]) + 1
    if min(left, right) - 1 >= PAD_MIN_SHARE * width and letterbox(left, width - right, False):
        return "left and right"
    return None


def _same_colour(a: Any, b: Any) -> bool:
    """Whether two colours (``[r, g, b]``, ``None`` for none read) are one surface's: within :data:`BAND_SAME_LEVELS`."""
    import numpy as np

    if a is None or b is None:
        return a is None and b is None
    return float(np.abs(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)).max()) <= BAND_SAME_LEVELS


@dataclass(frozen=True)
class Density:
    """How many of a source's own pixels land on one pixel of its quad at the render's size."""

    #: The source's size in pixels, and the quad's in the render's pixels (its width and height as drawn).
    source: tuple[int, int]
    quad: tuple[int, int]
    #: Source pixels per quad pixel along the axis the fit scales by: 1 is one to one, under :data:`MIN_DENSITY` soft.
    value: float
    #: The source size that would give one to one: the quad's size at the source's own proportions.
    needed: tuple[int, int]

    @property
    def soft(self) -> bool:
        return self.value < MIN_DENSITY

    @property
    def least(self) -> tuple[int, int]:
        """The smallest source of its proportions that is not soft: :data:`MIN_DENSITY` of one to one."""
        width, height = (side * MIN_DENSITY / self.value for side in self.source)
        return math.ceil(width - 1e-6), math.ceil(height - 1e-6)

    def as_dict(self) -> dict:
        return {
            "source": list(self.source),
            "quad": list(self.quad),
            "value": round(self.value, 3),
            "needed": list(self.needed),
            "least": list(self.least),
        }


def density(source_size: tuple[int, int], quad_w: float, quad_h: float, fit: str = "cover") -> Density | None:
    """The :class:`Density` of a *source_size* source fitted (*fit*) into a quad *quad_w* x *quad_h* render pixels wide.

    ``cover`` scales the source up until it fills the quad both ways (the
    larger of the two ratios), ``contain`` until it fits inside (the smaller):
    the pixels per pixel is one over that scale. ``None`` without a size.
    """
    src_w, src_h = (int(v) for v in source_size)
    if src_w <= 0 or src_h <= 0 or quad_w <= 0 or quad_h <= 0:
        return None
    ratios = (quad_w / src_w, quad_h / src_h)
    scale = min(ratios) if fit == "contain" else max(ratios)
    needed = (math.ceil(src_w * scale), math.ceil(src_h * scale))
    return Density((src_w, src_h), (round(quad_w), round(quad_h)), 1.0 / scale, needed)


def keyed_on(insert: InsertSource, first: Any) -> InsertSource:
    """*insert* keyed on its colour as *first* - its first footage frame (``H x W x 3``) - shows it; said in the log.

    A named colour is read off the surface (``screens.sample_key``); a
    ``#rrggbb`` is keyed as written. Both take the surface's spread
    (``screens.key_spread``), which widens the matte's tolerance to
    the light the surface is under. No key leaves the insert as it is.
    """
    import dataclasses

    from app.services.video_studio import screens

    if insert.key is None:
        return insert
    height, width = first.shape[:2]
    quad, _within = insert.quads(insert.first_frame)
    keyed = insert.key.sampled(first, [(x * width, y * height) for x, y in quad])
    used = keyed.key()
    label = insert.label or "A screen insert"
    spread = f", spread {keyed.spread:.3f} (tolerance {keyed.matte_tolerance():.2f})" if keyed.spread is not None else ""
    if insert.key.color not in dict(screens.KEY_CANDIDATES):
        logger.info("%s keys on %s as written%s", label, used.hex, spread)
    elif keyed.rgb is not None:
        logger.info(
            "%s keys on %s: the %s its surface shows on its first footage frame (frame %d)%s",
            label,
            used.hex,
            insert.key.color,
            insert.first_frame,
            spread,
        )
    else:
        logger.warning(
            "%s keys on the named %s itself (%s): its surface shows too little of that colour on its first footage frame",
            label,
            insert.key.color,
            used.hex,
        )
    return dataclasses.replace(insert, key=keyed)


def read_picture(path: str) -> Any:
    """An image insert's picture, read from its file: a PNG keeps its alpha."""
    with open(path, "rb") as source:
        return decode(source.read())


#: The box an SVG insert (a template's logo) is drawn into: a screen shows it at most about this large.
SVG_BOX = 2048


class SvgNotDrawnError(RuntimeError):
    """An SVG insert the rasteriser drew no PNG of; ``str()`` says why, in its words."""


async def svg_to_png(path: str, label: str = "A screen insert") -> str:
    """An SVG insert drawn to a PNG beside it by the bounded rasteriser, transparent where the SVG is: its path.

    :class:`SvgNotDrawnError` when the rasteriser drew nothing, its reason logged with *label* (what the
    findings call the insert) and carried in the error.
    """
    import asyncio
    from pathlib import Path

    from app.core.config import settings
    from app.services.svg_raster import render_svg_async

    data = await asyncio.to_thread(Path(path).read_bytes)
    drawn = await render_svg_async(data, box=SVG_BOX, strict=settings.svg_strict_rendering)
    if not drawn.png:
        reason = drawn.reason or "the rasteriser drew nothing"
        logger.warning("%s: its SVG (%s, %d bytes) was not drawn: %s", label, Path(path).name, len(data), reason)
        raise SvgNotDrawnError(reason)
    out = str(Path(path).with_suffix(".png"))
    await asyncio.to_thread(Path(out).write_bytes, drawn.png)
    return out
