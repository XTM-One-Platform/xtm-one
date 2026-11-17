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

"""A step's page changes paced at its take's frame rate.

The screencast hands over a frame only when the page painted and the one before
was taken (``scroll_pacing``): a few a second at pixel ratio 2. A tooltip
fading in over 0.2 s, a menu opening, a row taking its hover colour reached the
take as one frame before and one after - a pop, 1.0 to 1.4 frames a second over
a take of hovers and clicks.

So while a step that changes the page runs (``capture.MOTION_ACTIONS``: a
hover, a click, typing, a choice, a wait for an element, a key), the page notes
each element a transition or an animation runs on and each one the step changes
(``capture._MOTION_WATCH_JS``), and reads how long each moves from its computed
``transition-duration`` and ``animation-duration`` - their delays and iteration
counts with them -, the subtree changed included, at most
:data:`MOST_SECONDS`; a style a script changes again and again moves from its
first change to its last. The step's event keeps those windows (``motion``:
``[start, end, element]`` in seconds of the take).

Once recorded, each window whose keyframes are fewer than its take's rate is
drawn again at that rate (:func:`pace_transitions`): every frame the two
keyframes about its instant blended - the one at or before the window's start
standing for the page as it was from the window's start, the first one after
its end (within :data:`MAX_LATE`, before anything else moves) for the page as
the change left it from the window's end, those painted inside at their own
instants. An opacity or a colour transition is that blend; a transform reads
as a dissolve rather than a jump. A scroll that moved over the window keeps its
own frames (``scroll_pacing``), and a window whose keyframes are all alike
changed nothing on screen.

A pair of keyframes that differ by a discrete change - a label swapped for
another, icons shifted - is never
blended: a mix of the two draws both states at once, the text doubled
(``double_frames``' double image, carried into every render of the take). Where
the two differ, each draws edges of its own the other has not, and as many of
one's as of the other's, and neither paints the pixels that change in one
flat fill (:func:`discrete`): the earlier keyframe is held, then the later one cut
in where it stands (its paint, or the window's end). A fade or a move onto a
plain area (a highlight) draws structure on one side only, and a box fading in
over busy content (a tooltip over rows of text) paints most of what changes
in its own fill: both keep their blend. A click often changes several places at once - a counter swapped in a
toolbar while a panel slides in over the page -, and the panel's flat fill would outweigh the swapped label if the
pair were read as one change: so each place the two differ in is read on its own (:func:`held_regions`), its
changed pixels grouped along a line of text. One discrete place is enough: the whole window is then drawn as steps,
every frame byte-equal to one of its keyframes, the panel cut in with the label (#10039: holding the label alone
left the panel and a selection's rings half drawn over the page, 48 to 57 % mixes the ``double_image`` check read).
Content that appears in place of other content - a panel's text where a small loading spinner stood (#10100) - draws
edges on one side only, far more than the other, so it fails that balance; yet it is no fade of a box either: most
of the pixels that change are, on the other side, the very surface the content is drawn on - the panel's fill showing
between the strokes (:func:`appeared`) -, where a box fading in changes the whole box and leaves no surface between.
Such a place is held too, never drawn as the text rising over the spinner.
A keyframe the screencast itself painted mid-fade in such a window - a mix of its two neighbours
(``double_frames.read_blend``) - is dropped, its time kept (``painted_blends``). The window says how many frames
it held, not blended (``held``, ``held_not_blended``), as a canvas zoom does.

Each window a step keeps then says the rate its keyframes were painted at and
the rate it plays at (``painted``, ``fps``, ``paced``), as a scroll does - what
``scroll_pacing.take_rate`` reads.
"""

from __future__ import annotations

import bisect
import logging
import math
from collections.abc import Sequence
from typing import Any

from app.services.video_studio.scroll_pacing import (
    NOTHING_CHANGED,
    PAINTED_ENOUGH,
    FrameWriter,
    Keyframes,
    canvas_limited,
    page_frame_times,
    page_limited_fps,
    span_frames,
)

logger = logging.getLogger(__name__)

#: The longest a step's change is paced over, from when it begins (the page's watch caps it the same).
MOST_SECONDS = 2.0
#: A style changed again within this is one motion (a script animating it) - the page's watch joins them.
RUN_GAP = 0.1
#: The keyframe after a window stands for the page as its change left it when painted this soon after its end ...
MAX_LATE = 0.75
#: ... which the page's watch makes it paint, again and again until this long past it (``capture._MOTION_WATCH_JS``):
#: the screencast hands over a frame only when the page paints, and a finished transition paints nothing more.
PAINT_AFTER = 0.6
#: Two keyframes are the same picture - a change nothing painted - when fewer than this many of their pixels differ by
#: more than this many grey levels: a tooltip of a few hundred pixels is a change.
SAME_PIXELS = 64
CHANGED_LEVELS = 12.0
#: The grey levels of a softened gradient that make an edge, and the fewest edge pixels each keyframe of a pair must
#: draw of its own where the two differ for the change to be discrete (``double_frames`` reads a double image so).
EDGE_LEVELS = 8.0
MIN_OWN_EDGES = 40
#: ... and the smaller set of own edges at least this share of the larger: a swap draws about as much structure on
#: each side; a box fading in over a busy page draws a few edges of its own against the many it covers.
DISCRETE_BALANCE = 0.25
#: Pixels around the region the two keyframes differ in, read with it (an edge sits on the border of a change).
REGION_MARGIN = 4
#: The largest share of the changed pixels either keyframe may paint at one grey level (within :data:`CHANGED_LEVELS`)
#: for the change to be discrete: a swapped label paints them half in strokes, half in its background (0.5 to 0.62
#: measured on JPEG keyframes); a box fading in over rows of text paints them in its fill (0.76 to 0.92).
MOST_ONE_FILL = 0.7
#: Changed pixels this close (rows, columns) are one place: the glyphs and icons of a toolbar line join, a panel a
#: few rows above it stays apart.
GROUP_ROWS = 2 * REGION_MARGIN
GROUP_COLUMNS = 6 * REGION_MARGIN
#: The most places of a pair read on their own (the largest first); the rest blend.
MOST_PLACES = 32
#: :func:`appeared`: the least share of the changed pixels the plain side paints at the level of the surface the
#: drawing side's content stands on (its unchanged pixels between the strokes) - text drawn onto a panel's fill reads
#: 0.98; a box lit over a page 0.1 -, and the least share of the change's box that surface must cover (a box fading
#: in changes all of its own, leaving none: under 0.01).
APPEARED_SHARE = 0.8
SURFACE_SHARE = 0.1


def windows(raw: Any) -> list[tuple[float, float, list[str]]]:
    """A step's *raw* ``motion`` (``[[start, end, element], ...]`` in seconds of the take) merged where its windows
    meet, each with the elements that move in it, the longest first."""
    spans = sorted(
        (float(entry[0]), float(entry[1]), str(entry[2]) if len(entry) > 2 and entry[2] else "")
        for entry in raw or []
        if isinstance(entry, list | tuple) and len(entry) >= 2 and float(entry[1]) > float(entry[0])
    )
    merged: list[tuple[float, float, list[tuple[float, str]]]] = []
    for start, end, element in spans:
        if merged and start <= merged[-1][1]:
            first, last, elements = merged[-1]
            merged[-1] = (first, max(last, end), [*elements, (end - start, element)])
        else:
            merged.append((start, end, [(end - start, element)]))
    return [
        (start, end, list(dict.fromkeys(name for _length, name in sorted(elements, reverse=True) if name)))
        for start, end, elements in merged
    ]


def discrete(a: Any, b: Any) -> bool:
    """Whether two keyframes (grey arrays) differ by a discrete change no blend may stand for (see the module's
    docstring): where they differ by more than :data:`CHANGED_LEVELS`, each draws at least :data:`MIN_OWN_EDGES`
    edge pixels the other has not near, the smaller set at least :data:`DISCRETE_BALANCE` of the larger, and neither
    paints more than :data:`MOST_ONE_FILL` of the changed pixels at one grey level (a box filling them is a fade)."""
    import numpy as np
    from scipy.ndimage import binary_dilation, gaussian_filter

    first = np.asarray(a, dtype=np.float32)
    second = np.asarray(b, dtype=np.float32)
    changed = np.abs(first - second) > CHANGED_LEVELS
    if int(changed.sum()) < SAME_PIXELS:
        return False
    rows, cols = np.nonzero(changed.any(axis=1))[0], np.nonzero(changed.any(axis=0))[0]
    top, bottom = max(int(rows[0]) - REGION_MARGIN, 0), min(int(rows[-1]) + REGION_MARGIN + 1, first.shape[0])
    left, right = max(int(cols[0]) - REGION_MARGIN, 0), min(int(cols[-1]) + REGION_MARGIN + 1, first.shape[1])

    def edges(picture: Any) -> Any:
        dy, dx = np.gradient(gaussian_filter(picture[top:bottom, left:right], 1.0))
        return np.hypot(dy, dx) > EDGE_LEVELS

    one, other = edges(first), edges(second)
    near = binary_dilation(changed[top:bottom, left:right], iterations=REGION_MARGIN)
    own_one = int((one & ~binary_dilation(other, iterations=2) & near).sum())
    own_other = int((other & ~binary_dilation(one, iterations=2) & near).sum())
    if min(own_one, own_other) < MIN_OWN_EDGES:
        return False
    if min(own_one, own_other) < DISCRETE_BALANCE * max(own_one, own_other):
        return False
    bins = int(256 // CHANGED_LEVELS) + 1
    for picture in (first, second):
        levels = picture[changed]
        counts, _edges = np.histogram(levels, bins=bins, range=(0.0, CHANGED_LEVELS * bins))
        fill = (int(np.argmax(counts)) + 0.5) * CHANGED_LEVELS
        if float((np.abs(levels - fill) <= CHANGED_LEVELS).mean()) > MOST_ONE_FILL:
            return False
    return True


def appeared(a: Any, b: Any) -> bool:
    """Whether two keyframes (grey arrays) differ by content drawn on one side only - text appearing where a spinner
    stood, or going - rather than by a fade or a move (see the module's docstring): where they differ by more than
    :data:`CHANGED_LEVELS`, one draws at least :data:`MIN_OWN_EDGES` edge pixels of its own, the other fewer than
    :data:`DISCRETE_BALANCE` of that (a balanced pair is :func:`discrete`'s); the drawing side leaves at least
    :data:`SURFACE_SHARE` of the change's box unchanged between its strokes, and the plain side paints at least
    :data:`APPEARED_SHARE` of the changed pixels at that surface's level."""
    import numpy as np
    from scipy.ndimage import binary_dilation, gaussian_filter

    first = np.asarray(a, dtype=np.float32)
    second = np.asarray(b, dtype=np.float32)
    changed = np.abs(first - second) > CHANGED_LEVELS
    if int(changed.sum()) < SAME_PIXELS:
        return False
    rows, cols = np.nonzero(changed.any(axis=1))[0], np.nonzero(changed.any(axis=0))[0]
    top, bottom = max(int(rows[0]) - REGION_MARGIN, 0), min(int(rows[-1]) + REGION_MARGIN + 1, first.shape[0])
    left, right = max(int(cols[0]) - REGION_MARGIN, 0), min(int(cols[-1]) + REGION_MARGIN + 1, first.shape[1])

    def edges(picture: Any) -> Any:
        dy, dx = np.gradient(gaussian_filter(picture[top:bottom, left:right], 1.0))
        return np.hypot(dy, dx) > EDGE_LEVELS

    one, other = edges(first), edges(second)
    near = binary_dilation(changed[top:bottom, left:right], iterations=REGION_MARGIN)
    own_one = int((one & ~binary_dilation(other, iterations=2) & near).sum())
    own_other = int((other & ~binary_dilation(one, iterations=2) & near).sum())
    small, large = sorted((own_one, own_other))
    if large < MIN_OWN_EDGES or small >= DISCRETE_BALANCE * large:
        return False
    drawing, plain = (first, second) if own_one >= own_other else (second, first)
    # The surface the content is drawn on: the drawing side's pixels between its strokes the change left alone.
    box = (slice(int(rows[0]), int(rows[-1]) + 1), slice(int(cols[0]), int(cols[-1]) + 1))
    surface = drawing[box][~changed[box]]
    if surface.size < SURFACE_SHARE * changed[box].size:
        return False
    bins = int(256 // CHANGED_LEVELS) + 1
    counts, _edges = np.histogram(surface, bins=bins, range=(0.0, CHANGED_LEVELS * bins))
    level = (int(np.argmax(counts)) + 0.5) * CHANGED_LEVELS
    return float((np.abs(plain[changed] - level) <= CHANGED_LEVELS).mean()) >= APPEARED_SHARE


def held_regions(a: Any, b: Any) -> list[tuple[slice, slice]]:
    """The places two keyframes (grey arrays) differ in that are a discrete change (:func:`discrete`, read on each
    place alone), as row and column slices with :data:`REGION_MARGIN` around them: where a paced frame holds the
    earlier keyframe. Empty when the pair changed nothing or every place it changed may blend."""
    import numpy as np
    from scipy.ndimage import binary_dilation, find_objects, label

    first = np.asarray(a, dtype=np.float32)
    second = np.asarray(b, dtype=np.float32)
    changed = np.abs(first - second) > CHANGED_LEVELS
    if int(changed.sum()) < SAME_PIXELS:
        return []
    grouped = binary_dilation(changed, structure=np.ones((2 * GROUP_ROWS + 1, 2 * GROUP_COLUMNS + 1), dtype=bool))
    labels, count = label(grouped)
    if count == 1:
        boxes = find_objects(labels)
        held_whole = boxes[0] is not None and (discrete(first, second) or appeared(first, second))
        return [_widened(boxes[0], first.shape)] if held_whole else []
    places = []
    for number, box in enumerate(find_objects(labels), start=1):
        if box is None:
            continue
        inside = changed[box] & (labels[box] == number)
        size = int(inside.sum())
        if size >= SAME_PIXELS:
            places.append((size, number, box))
    held: list[tuple[slice, slice]] = []
    for _size, number, box in sorted(places, reverse=True)[:MOST_PLACES]:
        own = labels == number
        # The place alone: elsewhere both keyframes read as the earlier one, so only its own change is weighed.
        alone = np.where(own, second, first)
        if discrete(first, alone) or appeared(first, alone):
            held.append(_widened(box, first.shape))
    return held


def _widened(box: tuple[slice, slice], shape: tuple[int, ...]) -> tuple[slice, slice]:
    rows, cols = box
    return (
        slice(max(rows.start - REGION_MARGIN, 0), min(rows.stop + REGION_MARGIN, shape[0])),
        slice(max(cols.start - REGION_MARGIN, 0), min(cols.stop + REGION_MARGIN, shape[1])),
    )


def pace_transitions(
    reader: Keyframes,
    times: list[float],
    events: list[dict],
    *,
    fps: int,
    writer: FrameWriter,
    scrolls: Sequence[tuple[float, float]] = (),
    canvas_runs: Sequence[dict] = (),
) -> tuple[list[tuple[float, str]], set[int]]:
    """Each ``motion`` window of the take's *events* drawn again at *fps* where its keyframes (*reader*, painted at
    *times*) were fewer (see the module's docstring), and rewritten in place as the take keeps it: ``{t, element?,
    painted, fps, paced, frames?, why?}``. *scrolls* are the spans a scroll moved over: a window there keeps the
    scroll's frames. Answers the frames drawn and the keyframes they replace."""
    found = [(window, event) for event in events if event.get("motion") for window in windows(event["motion"])]
    for event in events:
        if event.get("motion"):
            event["motion"] = []
    starts = sorted([window[0] for window, _event in found] + [span[0] for span in scrolls])
    # The frames the page's renderer produced (``page_frames``, #10970), every step's together: a motion runs past its
    # step's read. ``None`` for a take recorded before the page counted them.
    page = page_frame_times(events)
    drawn: list[tuple[float, str]] = []
    replaced: set[int] = set()
    paced: list[tuple[float, float]] = []
    for serial, (window, event) in enumerate(sorted(found, key=lambda item: item[0][0])):
        start, end, elements = window
        later = starts[bisect.bisect_right(starts, start) :]
        limit = min([end + MAX_LATE, *(t for t in later if t >= end)])
        kept: dict[str, Any] = {"t": [round(start, 4), round(end, 4)], "paced": False}
        frames: list[tuple[float, str]] = []
        used: set[int] = set()
        if any(start < b and a < end for a, b in scrolls):
            kept["why"] = "a scroll moved over it"
        elif any(start < b and a < end for a, b in paced):
            kept["why"] = "it overlaps a transition paced before"
        else:
            try:
                kept, frames, used = _pace_window(
                    window,
                    reader,
                    times,
                    fps=fps,
                    writer=writer,
                    serial=serial,
                    limit=limit,
                    page=page,
                    canvas_runs=canvas_runs,
                )
            except Exception as exc:  # a window not paced keeps its keyframes: the take is never refused for it
                logger.warning("A transition of the take was not paced: %s", exc, exc_info=True)
                kept["why"] = "it could not be paced"
        if elements:
            kept["element"] = elements[0]
        if frames:
            paced.append((start, end))
            drawn.extend(frames)
            replaced |= used
        event["motion"].append(kept)
    return drawn, replaced


def _pace_window(
    window: tuple[float, float, list[str]],
    reader: Keyframes,
    times: list[float],
    *,
    fps: int,
    writer: FrameWriter,
    serial: int,
    limit: float,
    page: Sequence[float] | None = None,
    canvas_runs: Sequence[dict] = (),
) -> tuple[dict, list[tuple[float, str]], set[int]]:
    """One window paced (:func:`pace_transitions`): what the take keeps of it, the frames drawn for it and the keyframes
    they replace. *limit* is the latest the keyframe after it may be painted at and still show what its change left.
    *page* is the times the page's renderer produced a frame at (:func:`page_limited_fps`): a window the page itself
    drew under the take's rate, every one of its frames painted, is held at the page's own frames, never blended.
    *canvas_runs* are the canvas copies of the take (``alone.runs``): a window over a canvas the page drew under the
    rate, the screencast keeping every frame of it, follows its copy's classification (page-limited, #10970 retest);
    the page's frame loop runs at the display's rate whatever the canvas draws at, so it alone cannot tell."""
    import numpy as np

    start, end, _elements = window
    before, inside, after = span_frames(times, (start, end))
    painted = len(inside) / max(end - start, 1e-6)
    kept: dict[str, Any] = {
        "t": [round(start, 4), round(end, 4)],
        "painted": round(painted, 2),
        "fps": round(painted, 2),
        "paced": False,
    }
    if painted >= PAINTED_ENOUGH * fps:
        kept["why"] = "painted at its rate"
        return kept, [], set()
    limited = page_limited_fps(page, start, end, painted=painted, fps=fps)
    canvas, page_drew = canvas_limited(canvas_runs, start, end, painted=painted, fps=fps)
    if limited is None:
        limited = canvas
    late = after if after is not None and times[after] <= limit else None
    anchors = [*([before] if before is not None else []), *inside, *([late] if late is not None else [])]
    if (
        len(anchors) < 2
        or int((np.abs(reader.grey(anchors[0]) - reader.grey(anchors[-1])) > CHANGED_LEVELS).sum()) < SAME_PIXELS
    ):
        kept["why"] = NOTHING_CHANGED
        return kept, [], set()
    # A discrete change anywhere in the window (a label swapped, a selection toggled): every frame of it is one of its
    # keyframes, the earlier held until the later stands for the page, never two drawn at once - and a keyframe the
    # screencast painted mid-fade (a mix of its neighbours) is no keyframe of it: dropped, its time told. A window
    # with no discrete change keeps every keyframe and its blend (a fade, a zoom, a pan, a layout morph).
    stepped_anchors, painted_blends = _without_painted_blends(reader, anchors, times)
    stepped_at = _stands(stepped_anchors, times, before, late, start, end)
    stepped = any(
        stepped_at[k + 1] > stepped_at[k]
        and held_regions(reader.grey(stepped_anchors[k]), reader.grey(stepped_anchors[k + 1]))
        for k in range(len(stepped_anchors) - 1)
    )
    if stepped:
        anchors = stepped_anchors
    else:
        painted_blends = []
    # Drawn by the page under the take's rate (#10970): its own frames held, each until the next, never a blend that
    # would add a picture the page never drew.
    hold = stepped or limited is not None
    # A discrete change with no state between: every keyframe is the page before or the page after it (an icon swapped
    # on a click). Nothing moved between the two, so it is no transition: cut, never said synthesised nor refused.
    cut = stepped and discrete_cut([reader.grey(i) for i in anchors])
    at = _stands(anchors, times, before, late, start, end)
    held = 0
    drawn: list[tuple[float, str]] = []
    for n in range(math.ceil(start * fps - 1e-6), math.floor(end * fps + 1e-6) + 1):
        t = n / fps
        k = min(max(bisect.bisect_right(at, t) - 1, 0), len(anchors) - 1)
        if k == len(anchors) - 1 or at[k + 1] <= at[k]:
            picture = reader.rgb(anchors[k])
        elif hold:
            # The earlier keyframe as painted: a cut, never both states at once.
            picture = reader.rgb(anchors[k])
            held += 1
        else:
            share = min(max(round(256 * (t - at[k]) / (at[k + 1] - at[k])), 0), 256)
            low, high = reader.rgb(anchors[k]).astype(np.uint16), reader.rgb(anchors[k + 1]).astype(np.uint16)
            picture = ((low * (256 - share) + high * share + 128) >> 8).astype(np.uint8)
        name = f"m{serial:02d}{n:07d}.jpg"
        writer.write(picture, name)
        drawn.append((round(t, 6), name))
    writer.flush()
    if not drawn:
        kept["why"] = "shorter than a frame"
        return kept, [], set()
    kept.update({"fps": float(fps), "paced": True, "frames": len(drawn)})
    if held:
        kept.update({"held": held, "held_not_blended": True})
    if cut:
        kept["discrete"] = True
    if limited is not None:
        kept["page_limited"] = limited
    elif page_drew is not None:
        kept["page_drew"] = page_drew
    if painted_blends:
        kept["painted_blends"] = painted_blends
    return kept, drawn, set(inside)


def same(a: Any, b: Any) -> bool:
    """Whether two keyframes (grey arrays) are the same picture: fewer than :data:`SAME_PIXELS` pixels differ by more
    than :data:`CHANGED_LEVELS`."""
    import numpy as np

    return (
        int((np.abs(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32)) > CHANGED_LEVELS).sum())
        < SAME_PIXELS
    )


def discrete_cut(greys: Sequence[Any]) -> bool:
    """Whether a window's keyframes (grey arrays, in order) show a discrete change and no state between: each one
    the same picture as the first or as the last (:func:`same`), the first and the last differing. One keyframe in
    between unlike both is an intermediate state - a motion -, never a cut."""
    if len(greys) < 2 or same(greys[0], greys[-1]):
        return False
    return all(same(grey, greys[0]) or same(grey, greys[-1]) for grey in greys[1:-1])


def _stands(
    anchors: list[int], times: list[float], before: int | None, late: int | None, start: float, end: float
) -> list[float]:
    """Where each keyframe of *anchors* stands for the page: the one before from the window's start, the one after from
    its end, those painted inside at their own instants."""
    return [start if i == before else min(times[i], end) if i == late else times[i] for i in anchors]


def _without_painted_blends(reader: Keyframes, anchors: list[int], times: list[float]) -> tuple[list[int], list[float]]:
    """*anchors* without the keyframes painted inside the window that read as a mix of their neighbours (the browser
    caught mid-fade: ``double_frames.read_blend``), and the take times of those dropped. The first and the last anchor
    stand for the page before and after the change: always kept."""
    from app.services.video_studio.double_frames import read_blend

    kept = list(anchors)
    dropped: list[float] = []
    k = 1
    while k < len(kept) - 1:
        one, other = reader.grey(kept[k - 1]), reader.grey(kept[k + 1])
        read = read_blend(one, reader.grey(kept[k]), other)
        if read is not None and (read.double or (read.one_sided and appeared(one, other))):
            dropped.append(round(times[kept[k]], 4))
            del kept[k]
        else:
            k += 1
    return kept, dropped
