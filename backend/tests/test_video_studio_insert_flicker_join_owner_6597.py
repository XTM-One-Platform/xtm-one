"""One table of an insert's frames for its flicker, its flicker fact and its presence.

A 1080p60 render with two static screen inserts (fit contain, no key, untracked) said, under
``infra_once``, "insert flickers at t=1:17.57: ... not drawn on frame 4654, where it is drawn on the frames around it
(376 frames without it, 18 on its scene's join)", while the facts of the same render said "flicker 0 frame(s)" and
"its source present on all 381 encoded frame(s) it shows whole on". The frame at 1:17.57 is the full-screen recording
before the insert scene, under the zoom's blur: the monitor not on screen yet. The flicker check, the presence gate and
the flicker fact each worked out the insert's frames on their own.

Synthetic fixtures only - no browser, no ffmpeg. A full-screen recording joined to a clip scene carrying a static
unkeyed insert by a zoom, a whip, a slide and a flash at 60 fps, the clip coming in on a frame and half a frame past
one: its layer audited where the page shows any of it - never on the transition's first frame, the recording whole
there -, placed as the page's ``insertRects`` places it -

* the insert scene's flicker fact says 0, no ``insert_flicker``, no ``infra_once`` note, and the presence gate never
  reads the transition's first frame;
* the same with the insert withheld from the page log on one frame that is its scene's: ``insert_flicker`` on exactly
  that frame, its flicker fact 1, where the encoded frame there misses the insert - none where it shows it,
  nor on a frame of the transition in - and over random frames withheld and missed, a fact of 0 never stands beside a
  note;
* a render report whose flicker fact says 0 beside an ``insert_flicker`` note fails its self-consistency check.
"""

from __future__ import annotations

import io
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services.video_studio import insert_checks, render_answer, render_checks
from app.services.video_studio.frame_owners import FrameOwners
from app.services.video_studio.render_checks import FAIL, INSERT_FLICKER, INSERT_MISSING, Finding
from app.services.video_studio.screen_insert import Footage, InsertSource

FPS = 60
KEY = "insert/clip.0"
QUAD = [[0.3, 0.25], [0.7, 0.25], [0.7, 0.75], [0.3, 0.75]]
BOX = [0.3, 0.25, 0.7, 0.75]
LENGTH = 4.0
FADE = 0.6
#: The transitions of the report's video and the others whose first frames show nothing of the scene coming in.
KINDS = ("zoom", "whip", "slide_left", "flash")
#: Where the clip comes in, in frames past frame ``2 * FPS``: on it (the page's progress 0 there), and half a frame past.
PHASES = (0.0, 0.5)
W, H = 320, 180


def _in_out(t: float) -> float:
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _incoming(kind: str, p: float) -> float:
    """The opacity of the scene coming in at progress *p*, as the page's ``applyTransition`` sets its ``N`` - how much of
    its insert ``insertRects`` says it shows; a slide moves it in whole, out of the frame at first."""
    if p >= 1:
        return 1.0
    return {
        "zoom": 1 - (1 - p) ** 3,
        "whip": _clamp((p - 0.3) / 0.25),
        "slide_left": 1.0,
        "flash": _clamp((p - 0.44) / 0.12),
    }[kind]


def _document(kind: str, phase: float) -> dict:
    """A full-screen recording, then a clip scene of :data:`LENGTH` s with its static unkeyed insert to its end, coming in
    by *kind* (:data:`FADE` s) at frame ``2 * FPS + phase``, planned as ``timeline.plan`` rounds it."""
    start = round((2 * FPS + phase) / FPS, 4)
    insert = {"src": KEY, "quad": QUAD, "t0": 0.0, "t1": LENGTH, "to_end": True, "fit": "contain"}
    scenes = [
        {"id": "rec", "type": "recording", "start": 0.0, "end": round(start + FADE, 4), "transition_in": {"type": "cut", "duration": 0.0}},
        {
            "id": "clip",
            "type": "clip",
            "start": start,
            "end": round(start + LENGTH, 4),
            "transition_in": {"type": kind, "duration": FADE},
            "inserts": [insert],
        },
    ]
    duration = scenes[-1]["end"]
    return {"fps": FPS, "frames": math.ceil(duration * FPS), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": scenes}


def _inserts() -> dict[str, InsertSource]:
    return {
        KEY: InsertSource(
            "clip.png",
            "image",
            tuple(tuple(p) for p in QUAD),
            fit="contain",
            footage=Footage("clip.mp4", "video", float(FPS), "clip/clip"),
            label="Scene clip: its screen insert 0",
        )
    }


def _progress(document: dict, frame: int) -> float:
    clip = document["scenes"][1]
    return _clamp((frame / FPS - clip["start"]) / float(clip["transition_in"]["duration"]))


def _drawn(document: dict, *, withheld: set[int] = frozenset()) -> dict:
    """The clip insert's audits: placed on each frame of its scene as ``insertRects`` places it, its layer audited on
    each that shows any of it but the transition's first - the frame the report named, the recording whole there."""
    kind = document["scenes"][1]["transition_in"]["type"]
    scene = FrameOwners.of(document).frames_of(1)
    frames, placed = {}, {}
    for n in scene:
        shown = round(_incoming(kind, _progress(document, n)), 3)
        placed[str(n)] = [*BOX, shown]
        if n != scene.start and shown > 0 and n not in withheld:
            frames[str(n)] = {"visibility": 1.0}
    return {KEY: {"frames": frames, "evidence": {}, "placed": placed}}


def _placed(document: dict) -> dict[str, insert_checks.Placed]:
    return insert_checks.placed_inserts(document, _inserts(), owners=FrameOwners.of(document))


def _flickers(audits: dict, placed: dict) -> list[Finding]:
    return [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS) if finding.kind == INSERT_FLICKER]


def _rendered(document: dict, audits: dict, placed: dict, read: dict[int, bool]) -> tuple[dict, render_answer.RenderAnswer]:
    """The render's checks as ``render.render_video`` stores them - its insert findings and its presence gate's over the
    encoded frames *read* (frames its scene owns alone, each with whether it shows the insert, :func:`_clip_frame`),
    parted into the video's and the harness's, its inserts' reports - and the answer made of them."""
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    gate = insert_checks.PresenceGate(audits, placed, document, _inserts(), fps=FPS)
    pictures = {True: _clip_frame(), False: _clip_frame(shown=False)}
    for n, shows in sorted(read.items()):
        gate.read(n, pictures[shows], {need: SOURCE for need in gate.needs(n)})
    present = gate.result()
    found.extend(finding for finding, _proof in present.findings)
    kept, harness = render_checks.harness_defects(found, {})
    reports = insert_checks.reports(audits, placed, fps=FPS, presence=present.by_key)
    checks = render_checks.Checks(findings=kept, harness=harness, inserts=reports).as_dict()
    return checks, render_answer.of_checks(checks)


# ── The transition's first frame ──────────────────────────────────────────


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
def test_the_insert_scene_says_flicker_0_and_no_note_where_the_scene_before_is_whole(kind, phase):
    document = _document(kind, phase)
    owners = FrameOwners.of(document)
    placed = _placed(document)
    where = placed[KEY]
    first = owners.frames_of(1).start
    audits = _drawn(document)
    spots = insert_checks.placements_of(audits[KEY])
    # The page paints the clip's layer on the transition's first frame and the frame server drew none of it there: that
    # frame is the recording's, whichever side of a frame the plan's second lands (the slide places it whole there).
    assert first in where.expected_frames() and str(first) not in audits[KEY]["frames"]
    assert owners.owner(first) == 0 and not where.owns_frame(first, spots)
    if phase:
        assert first / FPS > document["scenes"][1]["start"] and _progress(document, first) > 0
    # Every frame judged is its scene's: alone past its transition in, or shown at the presence gate's floor there.
    judged = where.judged_frames(spots)
    assert judged and first not in judged
    assert all(n >= where.alone[0] or spots[n][4] >= insert_checks.PRESENCE_MIN_SHOWN for n in judged)
    checks, answer = _rendered(document, audits, placed, {where.alone[0]: True, where.alone[0] + FPS: True})
    (report,) = render_checks.insert_reports(checks)
    assert report["flicker_frames"] == 0 and report["missing_frames"] == 0
    texts = [measured.text for measured in insert_checks.classify(report)]
    assert "flicker 0 frame(s)" in texts and not any("not drawn" in text for text in texts)
    assert render_checks.findings_of(checks) == [] and render_checks.harness_of(checks) == []
    assert answer.status == render_answer.GREEN and answer.infra_once == []
    assert insert_checks.contradictions(checks) == []


def _ui(seed: int, size: tuple[int, int]) -> Image.Image:
    """A page of a product: a header, panels and lines of text, its detail everywhere."""
    rng = np.random.default_rng(seed)
    picture = Image.new("RGB", size, (18, 24, 36))
    draw = ImageDraw.Draw(picture)
    w, h = size
    draw.rectangle([0, 0, w, h // 10], fill=(40, 180, 200))
    for _ in range(14):
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(h // 8, h - 40))
        fill = tuple(int(v) for v in rng.integers(40, 230, 3))
        draw.rectangle([x, y, x + int(rng.integers(40, 160)), y + int(rng.integers(20, 90))], fill=fill)
    for row in range(h // 8, h, 14):
        draw.line([(w // 20, row), (w // 20 + int(rng.integers(w // 4, w - w // 10)), row)], fill=(220, 220, 220), width=2)
    return picture


SOURCE = _ui(1, (512, 360))
RECORDING = np.asarray(_ui(2, (W, H)))


def _encoded(rgb: np.ndarray) -> np.ndarray:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=90)
    return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))


def _clip_frame(*, shown: bool = True) -> np.ndarray:
    """An encoded frame of the clip alone: its footage, its insert on its quad - its footage there, not *shown*."""
    rgb = np.random.default_rng(7).integers(30, 140, (H, W, 3), dtype=np.uint8)
    x0, y0, x1, y1 = round(BOX[0] * W), round(BOX[1] * H), round(BOX[2] * W), round(BOX[3] * H)
    if shown:
        rgb[y0:y1, x0:x1] = np.asarray(SOURCE.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS))
    return _encoded(rgb)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
def test_the_presence_gate_reads_the_frames_the_flicker_reads_and_never_the_transition_s_first(kind, phase):
    document = _document(kind, phase)
    owners = FrameOwners.of(document)
    placed = _placed(document)
    first, alone = owners.frames_of(1).start, placed[KEY].alone[0]
    gate = insert_checks.PresenceGate(_drawn(document), placed, document, _inserts(), fps=FPS)
    # The transition's first frame shows the recording whole - placed whole by a slide; a frame its scene owns alone
    # shows the insert on its quad.
    gate.read(first, _encoded(RECORDING.copy()), {need: SOURCE for need in gate.needs(first)})
    gate.read(alone, _clip_frame(), {need: SOURCE for need in gate.needs(alone)})
    result = gate.result()
    assert [finding.message for finding, _proof in result.findings] == []
    assert gate.read_frames[KEY] == [alone]
    assert result.by_key[KEY]["missing"] == 0 and result.by_key[KEY]["faint"] == 1


# ── One frame withheld ────────────────────────────────────────────────────


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("which", ["transition", "alone"])
def test_the_insert_withheld_on_one_frame_its_scene_owns_fails_insert_flicker_on_exactly_that_frame(kind, phase, which):
    document = _document(kind, phase)
    placed = _placed(document)
    where = placed[KEY]
    spots = insert_checks.placements_of(_drawn(document)[KEY])
    judged = where.judged_frames(spots)
    # The first frame of its transition in shown at the floor, or a frame its scene owns alone.
    gone = judged[0] if which == "transition" else where.alone[0] + FPS
    assert where.owns_frame(gone, spots) and (gone < where.alone[0]) == (which == "transition")
    audits = _drawn(document, withheld={gone})
    # The page log misses it there - never a finding of its own.
    assert insert_checks.missing_frames(where, {int(n): a for n, a in audits[KEY]["frames"].items()}, None, spots) == [gone]
    assert _flickers(audits, placed) == []
    if which == "transition":
        # A transition's frame is never a flicker frame: nothing said, nothing counted.
        checks, answer = _rendered(document, audits, placed, {where.alone[0]: True})
        (report,) = render_checks.insert_reports(checks)
        assert report["flicker_frames"] == 0 and answer.infra_once == [] and insert_checks.contradictions(checks) == []
        return
    # The encoded frame there showing the insert: no note, a flicker of 0.
    checks, answer = _rendered(document, audits, placed, {gone: True})
    (report,) = render_checks.insert_reports(checks)
    assert report["flicker_frames"] == 0 and answer.infra_once == [] and insert_checks.contradictions(checks) == []
    # Missing it: the note, on exactly that frame.
    checks, answer = _rendered(document, audits, placed, {gone: False})
    (finding,) = [f for f in render_checks.harness_of(checks) if f["kind"] == INSERT_FLICKER]
    assert finding["data"]["frame"] == gone and finding["data"]["absent"] == 1 and finding["data"]["span"] == [round(gone / FPS, 3)] * 2
    assert finding["message"].startswith(f"insert flickers at t={render_checks.clock(gone / FPS)}: Scene clip: its screen insert 0 is not drawn on frame {gone},")
    (report,) = render_checks.insert_reports(checks)
    assert report["flicker_frames"] == 1 and report["missing_frames"] == 1
    # Nothing in the video causes it: the harness's note, said once beside a flicker fact of 1.
    (note,) = answer.infra_once
    assert "(insert_flicker)" in note and f"is not drawn on frame {gone}," in note
    assert "flicker 1 frame(s)" in [item["what"] for item in answer.facts]
    assert answer.status == render_answer.GREEN and insert_checks.contradictions(checks) == []


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("kind", KINDS)
def test_a_flicker_fact_of_0_never_stands_beside_an_insert_flicker_note(kind, phase):
    """The note and the fact are one count (``insert_checks.owned_flicker``, the presence gate's ``undrawn``): over random
    frames withheld from the page log - its scene's or not -, some of them missed by the encoded frame too, a
    note or a block is said exactly when the fact is past 0 - a block of the video once more than one frame is not drawn
    (``render_checks.HARNESS_MAX_FRAMES``), the harness's note on one."""
    document = _document(kind, phase)
    placed = _placed(document)
    scene = FrameOwners.of(document).frames_of(1)
    alone = placed[KEY].alone[0]
    rng = np.random.default_rng([6597, KINDS.index(kind), int(phase * 2)])
    for _ in range(12):
        withheld = {int(n) for n in rng.choice(list(scene), size=int(rng.choice([0, 1, 2, 5])), replace=False)}
        read = {n: bool(rng.random() < 0.5) for n in withheld if n >= alone}
        checks, answer = _rendered(document, _drawn(document, withheld=withheld), placed, read)
        (report,) = render_checks.insert_reports(checks)
        notes = [line for line in answer.infra_once if "(insert_flicker)" in line]
        blocks = [f for f in render_checks.findings_of(checks) if f.get("kind") == INSERT_MISSING and (f.get("data") or {}).get("gate") == "flicker"]
        assert (report["flicker_frames"] > 0) == bool(notes or blocks)
        assert not blocks or report["flicker_frames"] > render_checks.HARNESS_MAX_FRAMES
        assert insert_checks.contradictions(checks) == []


# ── The report's self-consistency ─────────────────────────────────────────


def test_a_report_with_a_flicker_0_fact_and_an_insert_flicker_note_fails_its_self_consistency_check():
    document = _document("zoom", 0.0)
    placed = _placed(document)
    checks, _answer = _rendered(document, _drawn(document), placed, {placed[KEY].alone[0]: True})
    assert insert_checks.contradictions(checks) == []
    (report,) = render_checks.insert_reports(checks)
    assert report["flicker_frames"] == 0
    # The report's note, filed beside its insert's flicker 0 fact: the harness's, as it was said (one frame).
    unexplained = {"kind": "nothing", "text": "nothing in the video causes it: render again, and report the render if it stays"}
    note = Finding(
        INSERT_FLICKER,
        77.567,
        77.567,
        "clip",
        "insert flickers at t=1:17.57: Scene clip: its screen insert 0 is not drawn on frame 4654, where it is drawn on the "
        "frames around it - its surface shows as the footage has it there (1 frame without it, 1 on its scene's join)",
        FAIL,
        {"insert": KEY, "frame": 4654, "absent": 1, "span": [77.567, 77.567], "gate": "flicker", "action": unexplained},
    )
    _kept, harness = render_checks.harness_defects([note], {})
    (said,) = insert_checks.contradictions({**checks, "harness": [defect.as_dict() for defect in harness]})
    assert said == f"insert {KEY}: insert_flicker at t=1:17.57 beside its report's flicker of 0 frames"
    # The same among the video's findings fails it too.
    found = Finding(INSERT_FLICKER, 77.567, 77.567, "clip", "insert flickers at t=1:17.57", FAIL, {"insert": KEY, "frames": 3})
    assert len(insert_checks.contradictions({**checks, "findings": [found.as_dict()]})) == 1
    # An insert_flicker on another insert, or beside a fact past 0, is none.
    elsewhere = Finding(INSERT_FLICKER, 1.0, 1.0, "rec", "insert flickers at t=0:01.00", FAIL, {"insert": "insert/rec.0", "frames": 2})
    assert insert_checks.contradictions({**checks, "findings": [elsewhere.as_dict()]}) == []
    counted = {**checks, "inserts": [{**report, "flicker_frames": 1}], "findings": [found.as_dict()]}
    assert insert_checks.contradictions(counted) == []
