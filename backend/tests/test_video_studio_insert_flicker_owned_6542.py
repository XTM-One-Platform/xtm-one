"""An insert's layer judged drawn on the frames its scene owns and shows it on, never on the scene before.

A 1080p60 render said, on two insert scenes, "insert flickers at t=...: ... is not drawn on frame N, where it is drawn
on the frames around it", with the facts "not drawn on 61 frame(s) the page draws it on" and "not drawn on 55" on its
scene's joins. The frame at that time is the scene before whole - a full-screen recording, no monitor: the blur into the
insert scene had not begun. A scene coming in on a frame is at the page's progress 0 there (``applyTransition``): its
layer at opacity 0, slid out of the frame or masked out - the scene before painted whole -, and the first frames after
it show almost nothing of the insert.

Synthetic fixtures only - no browser, no ffmpeg. A full-screen recording joined to a clip scene with a keyed insert held
over it, by each transition and by a cut, at 30 and 60 fps, the clip coming in on a frame and half a frame past one (the
join's first frame then the recording's own): its layer audited on the frames that show any of it, and placed as the
page's ``insertRects`` places it -

* no frame "not drawn" in the page log: a frame the scene before still paints whole is never judged;
* the same with the insert withheld from one frame it is judged on: the page log misses exactly that frame - which
  is no finding on its own: the presence gate says it where the encoded frame reads it missing
  (``test_video_studio_insert_infra_notes_6596``), and the report says what that gate counted;
* a frame of the transition in showing it under ``PRESENCE_MIN_SHOWN``, withheld: never counted, as the presence gate
  never judges it;
* an insert on the scene the transition leaves: its transition out's frames judged only where the page still
  shows its quad at the floor - withheld under it, never counted; at it, missed by the page log;
* the joins line says the frames measured and the residual key alone, the presence gate's join spans kept in
  the MP4's seconds for debugging.
"""

from __future__ import annotations

import math

import pytest

from app.services.video_studio import compositor_page, insert_checks, timeline
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import INSERT_FLICKER
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

KEY = "insert/clip.0"
QUAD = [[0.3, 0.25], [0.7, 0.25], [0.7, 0.75], [0.3, 0.75]]
BOX = [0.3, 0.25, 0.7, 0.75]
LENGTH = 3.0
FADE = 0.6
#: Every transition a scene comes in by (``spec.TransitionType`` but ``auto``, which the planner resolves to one of them).
KINDS = ("cut", "crossfade", "fade_black", "slide_left", "slide_up", "zoom", "blur", "wipe", "whip", "flash")
#: Where the clip comes in, in frames past frame ``2 * fps``: on it (the page's progress 0 there), and half a frame past.
PHASES = (0.0, 0.5)


def _in_out(t: float) -> float:
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _out3(t: float) -> float:
    return 1 - (1 - t) ** 3


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _incoming(kind: str, p: float) -> float:
    """The opacity of the scene coming in at progress *p* - how much of its insert ``insertRects`` says it shows - as the
    page's ``applyTransition`` sets its ``N`` (the brand's ``move`` easing ``in_out``); whole once it is in."""
    if p >= 1:
        return 1.0
    return {
        "fade_black": _out3(_clamp(p * 2 - 1)),
        "slide_left": 1.0,
        "slide_up": 1.0,
        "wipe": 1.0,
        "zoom": _out3(p),
        "whip": _clamp((p - 0.3) / 0.25),
        "flash": _clamp((p - 0.44) / 0.12),
    }.get(kind, _in_out(p))


def _keyed(fps: int, *, to_end: bool = True) -> dict:
    payload = {"src": KEY, "quad": QUAD, "t0": 0.0, "t1": LENGTH, "keyed": True, "matte": {"fps": fps, "frames": 400}}
    if to_end:
        payload["to_end"] = True
    timeline.hold_surfaces([payload], LENGTH)
    return payload


def _document(fps: int, kind: str, phase: float) -> dict:
    """A full-screen recording, then a clip scene of :data:`LENGTH` s with its keyed insert held over it, coming in by
    *kind* (:data:`FADE` s, a cut none) at frame ``2 * fps + phase``, planned as ``timeline.plan`` rounds it."""
    seconds = 0.0 if kind == "cut" else FADE
    start = round((2 * fps + phase) / fps, 4)
    scenes = [
        {"id": "rec", "type": "recording", "start": 0.0, "end": round(start + seconds, 4), "transition_in": {"type": "cut", "duration": 0.0}},
        {
            "id": "clip",
            "type": "clip",
            "start": start,
            "end": round(start + LENGTH, 4),
            "transition_in": {"type": kind, "duration": seconds},
            "inserts": [_keyed(fps)],
        },
    ]
    duration = scenes[-1]["end"]
    return {"fps": fps, "frames": math.ceil(duration * fps), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _inserts(fps: int) -> dict[str, InsertSource]:
    return {
        KEY: InsertSource(
            "clip.png",
            "image",
            tuple(tuple(p) for p in QUAD),
            key=KeySpec("green", 0.4, 3.0),
            footage=Footage("clip.mp4", "video", float(fps), "clip/clip"),
            label="Scene clip: its screen insert 0",
        )
    }


def _progress(document: dict, frame: int) -> float:
    """The clip's progress into its scene on output *frame*, as the page reads it: ``(T - start) / duration``."""
    clip = document["scenes"][1]
    seconds = float(clip["transition_in"]["duration"])
    return _clamp((frame / document["fps"] - clip["start"]) / seconds) if seconds > 0 else 1.0


def _drawn(document: dict, *, withheld: set[int] = frozenset()) -> dict:
    """The clip insert's audits: placed on each frame of its scene as ``insertRects`` places it (its scene's opacity
    there), its layer audited on each that shows any of it - none where the recording is still painted whole."""
    kind = document["scenes"][1]["transition_in"]["type"]
    frames, placed = {}, {}
    for n in FrameOwners.of(document).frames_of(1):
        p = _progress(document, n)
        shown = round(_incoming(kind, p), 3)
        placed[str(n)] = [*BOX, shown]
        if p > 0 and shown > 0 and n not in withheld:
            frames[str(n)] = {"visibility": 1.0}
    return {KEY: {"frames": frames, "evidence": {}, "placed": placed}}


def _undrawn(audits: dict, placed: dict) -> list[int]:
    """The frames the page log misses the clip insert's layer on (``insert_checks.missing_frames``)."""
    part = audits[KEY]
    frames = {int(n): audit for n, audit in part["frames"].items()}
    return insert_checks.missing_frames(placed[KEY], frames, None, insert_checks.placements_of(part))


def _flickers(audits: dict, placed: dict, fps: int) -> list:
    return [finding for finding, _proof in insert_checks.findings(audits, placed, fps=fps) if finding.kind == INSERT_FLICKER]


def _report(audits: dict, placed: dict, fps: int, **kwargs) -> dict:
    return next(r for r in insert_checks.reports(audits, placed, fps=fps, **kwargs) if r["key"] == KEY)


# ── The frames the scene before still paints whole ────────────────────────


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fps", [30, 60])
def test_a_frame_the_scene_before_still_paints_whole_is_never_judged(fps, kind, phase):
    document = _document(fps, kind, phase)
    owners = FrameOwners.of(document)
    placed = insert_checks.placed_inserts(document, _inserts(fps), owners=owners)
    audits = _drawn(document)
    where = placed[KEY]
    first = owners.frames_of(1).start
    start = document["scenes"][1]["start"]
    if phase:
        # The join's first frame by the plan's seconds is the recording's own: the scene's start less one frame.
        assert math.floor(start * fps) == first - 1 and owners.owner(first - 1) == 0
    elif kind != "cut":
        # The clip's first frame is at its progress 0: the page paints the recording whole there.
        assert first / fps == start and _progress(document, first) == 0.0
        # What the check read earlier - every frame the page draws the layer on - starts on that frame: the report.
        unseen = [n for n in where.expected_frames() if str(n) not in audits[KEY]["frames"]]
        assert unseen[0] == first
    if kind != "cut":
        # The first frame of the transition in is the recording's, whichever side of a frame the plan starts it.
        assert owners.owner(first) == 0 and where.owns[0] == first + 1 and not where.owns_frame(first, {first: [*BOX, 1.0]})
        assert where.alone == (owners.frames_of(0).stop, where.owns[1])
    assert _undrawn(audits, placed) == [] and _flickers(audits, placed, fps) == []
    report = _report(audits, placed, fps, presence={KEY: {"frames": 10, "missing": 0, "unjudged": 0, "joins": 0, "joins_missing": 0}})
    assert report["missing_frames"] == 0 and report["flicker_frames"] == 0
    assert not any("not drawn" in m.text for m in insert_checks.classify(report))
    # Every frame it is judged on its scene's own, past its progress 0, shown at the presence gate's floor or more.
    spots = insert_checks.placements_of(audits[KEY])
    judged = where.judged_frames(spots)
    assert judged and all(owners.owner(n) == 1 for n in judged)
    assert all(_progress(document, n) > 0 and spots[n][4] >= insert_checks.PRESENCE_MIN_SHOWN for n in judged)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fps", [30, 60])
@pytest.mark.parametrize("which", ["first", "middle"])
def test_the_insert_withheld_on_one_frame_its_scene_owns_is_missed_by_the_page_log_on_exactly_that_frame(fps, kind, phase, which):
    document = _document(fps, kind, phase)
    placed = insert_checks.placed_inserts(document, _inserts(fps))
    judged = placed[KEY].judged_frames(insert_checks.placements_of(_drawn(document)[KEY]))
    # The first frame it is judged on - the first its transition shows it at the floor on, a cut's first - or one inside.
    gone = judged[0] if which == "first" else judged[len(judged) // 2]
    audits = _drawn(document, withheld={gone})
    assert _undrawn(audits, placed) == [gone]
    # The page log alone is no finding nor counted: the presence gate says it off the encoded frame, and the
    # report counts what that gate read.
    assert _flickers(audits, placed, fps) == []
    alone = _report(audits, placed, fps)
    assert alone.get("missing_frames") is None and alone["flicker_frames"] == 0
    read = {KEY: {"frames": 10, "missing": 1, "unjudged": 0, "joins": 0, "joins_missing": 0, "undrawn": 1}}
    report = _report(audits, placed, fps, presence=read)
    # Counted in its flicker fact, never said apart.
    assert report["missing_frames"] == 1 and report["flicker_frames"] == 1
    texts = [m.text for m in insert_checks.classify(report, {INSERT_FLICKER})]
    assert "flicker 1 frame(s)" in texts and not any("not drawn" in text for text in texts)


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind not in ("cut", "slide_left", "slide_up", "wipe")])
@pytest.mark.parametrize("fps", [30, 60])
def test_a_frame_of_the_transition_in_showing_it_under_the_floor_is_never_counted(fps, kind):
    document = _document(fps, kind, 0.5)
    placed = insert_checks.placed_inserts(document, _inserts(fps))
    drawn = _drawn(document)[KEY]
    faint = [n for n, spot in insert_checks.placements_of(drawn).items() if 0 < spot[4] < insert_checks.PRESENCE_MIN_SHOWN]
    assert faint, "the transition in shows the insert faint on some frames"
    audits = _drawn(document, withheld=set(faint))
    assert _undrawn(audits, placed) == []


# ── The scene the transition leaves ───────────────────────────────────────


@pytest.mark.parametrize("fps", [30, 60])
def test_an_insert_s_transition_out_is_judged_only_where_the_page_shows_it_at_the_floor(fps):
    """Its transition out's frames are the scene coming in's (``FrameOwners.owner``) but its first, the clip whole there;
    its layer is judged drawn on them where the page still shows its quad at the presence gate's floor, as the presence
    gate judges its source there (``Placed.owns_frame``) - never under it."""
    end = round(LENGTH, 4)
    scenes = [
        {"id": "clip", "type": "clip", "start": 0.0, "end": end, "transition_in": {"type": "cut", "duration": 0.0}, "inserts": [_keyed(fps)]},
        {
            "id": "rec",
            "type": "recording",
            "start": round(end - FADE, 4),
            "end": round(end - FADE + 3.0, 4),
            "transition_in": {"type": "crossfade", "duration": FADE},
        },
    ]
    duration = scenes[-1]["end"]
    document = {"fps": fps, "frames": math.ceil(duration * fps), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}
    owners = FrameOwners.of(document)
    placed = insert_checks.placed_inserts(document, _inserts(fps), owners=owners)
    mine, leaving = owners.owned(0), range(owners.frames_of(1).start, owners.frames_of(0).stop)
    assert placed[KEY].owns == (mine.start, mine.stop) and mine.stop == leaving.start + 1 and len(leaving) == round(FADE * fps)
    assert placed[KEY].alone == placed[KEY].owns and owners.owner(leaving.start) == 0
    assert placed[KEY].expected_frames()[-1] == leaving[-1]

    def shown(n: int) -> float:
        # The crossfade's incoming layer at the page's opacity e over the clip: its quad shown 1 - e (``insertRects``).
        return round(1.0 - _in_out(_clamp((n / fps - scenes[1]["start"]) / FADE)), 3) if n in leaving else 1.0

    frames = {str(n): {"visibility": 1.0} for n in owners.frames_of(0)}
    audits = {KEY: {"frames": frames, "evidence": {}, "placed": {str(n): [*BOX, shown(n)] for n in owners.frames_of(0)}}}
    assert _undrawn(audits, placed) == [] and _report(audits, placed, fps)["flicker_frames"] == 0
    faint = [n for n in leaving if shown(n) < insert_checks.PRESENCE_MIN_SHOWN]
    seen = [n for n in leaving[1:] if shown(n) >= insert_checks.PRESENCE_MIN_SHOWN]
    assert faint and seen
    # Withheld where the page shows it under the floor: never counted.
    for n in faint:
        frames.pop(str(n))
    assert _undrawn(audits, placed) == []
    # Withheld where the page still shows it at the floor, or on a frame its scene owns: missed by the page log on
    # exactly that frame - for the presence gate to read off the encoded frame, never a finding of the audits.
    for gone in (seen[-1], mine[-1]):
        taken = frames.pop(str(gone))
        assert _undrawn(audits, placed) == [gone] and _flickers(audits, placed, fps) == []
        frames[str(gone)] = taken


# ── The joins line ────────────────────────────────────────────────────────


def test_the_joins_line_says_the_frames_measured_and_the_presence_gate_s_spans_are_kept_for_debugging():
    document = _document(30, "blur", 0.0)
    placed = insert_checks.placed_inserts(document, _inserts(30))
    audits = _drawn(document)
    # A scene preview's window from 1.0 s of the plan: its MP4 starts there.
    presence = {
        KEY: {
            "frames": 80,
            "missing": 2,
            "unjudged": 0,
            "joins": 14,
            "joins_missing": 2,
            "join_spans": {"present": [[2.433, 2.567]], "missing": [[2.367, 2.4]]},
        }
    }
    report = _report(audits, placed, 30, offset=1.0, presence=presence)
    assert "missing" not in report["joins"]
    # The presence gate's join spans, in the MP4's seconds, kept for debugging: the joins line says none of them.
    assert report["joins"]["read"] == {"present": [[1.433, 1.567]], "missing": [[1.367, 1.4]]}
    (line,) = [m.text for m in insert_checks.classify(report) if m.text.startswith("on its scene's joins with the scenes beside it: ")]
    assert line == (
        f"on its scene's joins with the scenes beside it: {report['joins']['frames']} frame(s) measured, residual key 0.00% "
        "of the quad at most"
    )
    # The presence summary keeps its spans in the plan: what another drawing of the same join is held to.
    assert report["presence"]["join_spans"] == presence[KEY]["join_spans"]


def test_the_page_s_progress_and_incoming_opacity_are_the_ones_the_fixture_reads():
    """The table gives a transition's first frame - the page's progress 0, or a hair past it - to the scene before
    (``FrameOwners.owner``); the fixture's :func:`_incoming` reads the page's ``N``."""
    script = compositor_page.page_html("")
    for line in (
        "const p = tr.duration > 0 ? clamp((t - sc.start) / tr.duration, 0, 1) : 1;",
        "const e = easeOf('move')(p);",
        "N.opacity = String(out3(clamp(p * 2 - 1, 0, 1)));",
        "N.opacity = String(out3(p));",
        "N.filter = `blur(${(1 - e) * 14}px)`; N.opacity = String(e);",
        "N.opacity = String(clamp((p - 0.3) / 0.25, 0, 1));",
        "const swap = clamp((p - 0.44) / 0.12, 0, 1);",
        "N.opacity = String(e); N.transform = `scale(${1.03 - 0.03 * e})`;",
    ):
        assert line in script, line
