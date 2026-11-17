"""Every screen insert gate holds what the insert can fix, and nothing it cannot.

Synthetic fixtures only - no provider, no browser, no database. Each is composed through ``screen_insert.compose``,
the render's own function, as the frame server draws a static keyed insert (its surface filled once off its first
footage frame), and read back as the render's findings and report (``insert_checks``):

* F1 a flat panel at 35 degrees of yaw, its sides 25% apart, no bow: passes - seen at an angle is a fact, by design;
* F2 a cylindrical panel bowing 8%: the flatness finding alone, at save and on the frames drawn;
* F3 a keyed panel under a screenshot with a 100%-green ring: the ring is the source's own, no residual key, no halo;
* F4 F3 with a hand crossing the ring over 40% of the quad: everything passes, the colours not compared, said;
* F5 a panel whose green runs 1% past its bezel: no residual key, the margin filled with the bezel's colour;
* F6 a 16:9 source on a 4:3 trapezoid, contain: the bands filled, no residual key;
* F7 a clip whose camera moves by intent, its scene previewed green: ``camera_moved`` cleared;
* F8 F3 keyed at tolerance 0.4 and 0.88: each read at its own tolerance and drawn anew, never from the other's
  segments, both pass.

Every gate on the pixels it owns, one measure at save, preview and render:

* F9 a flat panel, a round object over the lower part of one edge on every frame: passes, that edge mostly covered and
  not judged, the object kept in front, the save's reading and the render's the same;
* F10 a cylindrical panel with the same occlusion: the flatness finding alone, on the edges left visible;
* F11 one shot measured at save, previewed at 1080p and rendered at 2160p: the same verdicts;
* F12 a source UI whose text reads 4 px on the 1080p frame: a legibility finding naming the viewport;
* F13 a shot whose regeneration failed, its previous take playing: said once, advice, never in the render gate;
* F14 a hand crossing a dark UI area: no halo, the colours compared and passing.

And the pieces: a halo needs five frames, a finding no key or light setting moved says ``not a setting`` and its
cause, a curved surface folds the findings that follow from it, a render finding that a save or a preview of the same
shot passed disagrees with it, and a segment's digest moves with every field of its inserts.

One verdict from the shot's own lint to its render: the lint of a shot carrying an insert gives the save's and
the render's verdict on F9, F10 and F11, a bow is never confirmed by one reading, a render finding the shot's lint
passed on the take that plays disagrees with it, and a report carries the version the studio keys it by.

The key's spill taken off the edge of what stands in front, in the compositor:

* F15 a matte key panel a model painted off the primary, a furry subject whose edge holds a 20% tint of it: no halo
  (under 2%), the subject's interior colours unchanged (delta E under 1), the share corrected said frame by frame;
* F16 F15 with ``despill.edge`` off: the halo fails, saying the edge spill with the despill off, its worst stretch kept;
* F17 the insert's light wrapped onto the subject's edge on a 2 px band of a 1080p frame only.

And the pieces: a halo after the despill says its width and advises widening it, never past its bound, a drawing at
another despill that left the same halo says ``not a setting``, the save says the edge spill before any preview, and
the despill travels to every drawing of the insert.

The subject's own colour is decided on the subject, never on a per-pixel share of the frame:

* F18 a pink-nosed subject crossing a green-keyed panel from outside the frame (its tip alone, motion-blurred): the halo
  passes, its pink said as its own over the clip and on the edge, and kept on the frame;
* F19 a grey subject with a real 3 px green fringe: the halo fails, the fringe never read as its colour;
* F20 a subject in a magenta jacket, no clamp on it: passes, the jacket's magenta left out and said;
* F21 the same with an inverting clamp on a tenth of its edge: fails, counted where the clamp touched it and named so.

And the pieces: own hues an author declares travel from the spec to every drawing and are never counted, the clamp's
included; the save reads the subject once by its clip, and the render takes it for every pane of the insert.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from app.services.video_studio import (
    compositor,
    encoder,
    insert_bezels,
    insert_builds,
    insert_checks,
    render_checks,
    render_ranges,
    screen_insert,
    screens,
    surface_refine,
    timeline,
)
from app.services.video_studio.render_checks import INSERT_FLATNESS, INSERT_HALO, INSERT_RESIDUAL_KEY
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 960, 540
FPS = 24.0
FRAMES = 8
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
GREEN = (32, 230, 40)
BEZEL = (28, 28, 32)
#: A rectangular screen, and a panel at 35 degrees of yaw: its left side 25% taller than its right, no edge bowing.
SCREEN = np.array([(300, 130), (660, 130), (660, 400), (300, 400)], dtype=float)
YAWED = np.array([(300, 140), (640, 167), (640, 383), (300, 410)], dtype=float)
#: A 4:3 trapezoid: 360 wide, 300 tall on its left and 240 on its right.
TRAPEZOID = np.array([(300, 120), (660, 150), (660, 390), (300, 420)], dtype=float)


def _quad(quad_px: np.ndarray, width: int = W, height: int = H) -> tuple:
    return tuple((float(x) / width, float(y) / height) for x, y in quad_px)


def _wall(seed: int, width: int = W, height: int = H) -> Image.Image:
    rng = np.random.default_rng(seed)
    return Image.fromarray((rng.random((height, width, 3)) * 20 + 95).astype(np.uint8))


#: The far side of a panel the model painted unevenly: its green leaning to cyan.
CYAN_LEAN = (25, 215, 150)

def _blurred(picture: Image.Image, seed: int) -> np.ndarray:
    """*picture* through a lens and an encoder: blurred, with noise."""
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0))).astype(np.float32)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 3.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _panel(quad_px: np.ndarray, *, seed: int, hand: bool = False, lean: tuple | None = None) -> np.ndarray:
    """A noisy wall, a dark bezel around a flat green panel at *quad_px*; with *lean*, its green turning to that colour
    across it; with *hand*, a hand rising into it through its bottom edge over 40% of it."""
    picture = _wall(seed)
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(quad_px, 8.0)], fill=BEZEL)
    if lean is None:
        draw.polygon([tuple(p) for p in quad_px], fill=GREEN)
    else:
        left, right = float(quad_px[:, 0].min()), float(quad_px[:, 0].max())
        t = np.clip((np.arange(W, dtype=np.float32) - left) / (right - left), 0.0, 1.0)[None, :, None]
        ramp = (1 - t) * np.array(GREEN, dtype=np.float32) + t * np.array(lean, dtype=np.float32)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon([tuple(p) for p in quad_px], fill=255)
        picture = Image.composite(Image.fromarray(np.repeat(ramp, H, axis=0).astype(np.uint8)), picture, mask)
        draw = ImageDraw.Draw(picture)
    if hand:
        skin = (196, 148, 118)
        draw.rounded_rectangle((352, 250, 600, 470), radius=40, fill=skin)
        for finger in range(4):
            x = 372 + finger * 56
            draw.rounded_rectangle((x, 205 + (finger % 2) * 12, x + 36, 270), radius=16, fill=skin)
    return _blurred(picture, seed)


def _spilled(seed: int, *, bezel: int = 4) -> np.ndarray:
    """The rectangular screen with a thin bezel *bezel* px wide, the panel's green running past it to 1% of the
    frame's width from the screen's edge."""
    picture = _wall(seed)
    draw = ImageDraw.Draw(picture)
    (x0, y0), (x1, y1) = SCREEN[0], SCREEN[2]
    spill = round(0.01 * W)
    draw.rectangle((x0 - spill, y0 - spill, x1 + spill, y1 + spill), fill=GREEN)
    draw.rectangle((x0 - bezel, y0 - bezel, x1 + bezel, y1 + bezel), fill=BEZEL)
    draw.rectangle((x0, y0, x1, y1), fill=GREEN)
    return _blurred(picture, seed)


def _screenshot(*, ring: bool = False, size: tuple[int, int] = (1200, 900)) -> Image.Image:
    """A dark product UI with lines of text on a flat ground; with *ring*, a donut chart in a 100% green."""
    width, height = size
    picture = Image.new("RGB", size, (22, 26, 34))
    draw = ImageDraw.Draw(picture)
    for row in range(60, height - 60, 36):
        draw.rectangle((80, row, width // 2 - 140, row + 12), fill=(200, 205, 215))
    if ring:
        cx, cy, r = width * 3 // 4, height // 2, height // 5
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(0, 255, 0), width=max(8, r // 4))
    return picture


def _insert(quad_px: np.ndarray, *, tolerance: float = 0.4, feather: float = 3.0, fit: str = "cover") -> InsertSource:
    quad = _quad(quad_px)
    return InsertSource(
        "p.png",
        "image",
        quad,
        fit=fit,
        key=KeySpec("green", tolerance, feather),
        footage=Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label=LABEL,
    )


def _drawn(
    insert: InsertSource, frames: list[np.ndarray], picture: Image.Image, size: tuple[int, int] = (W, H), *, evidence: bool = False
) -> list:
    """Each frame of a static keyed insert as the frame server draws it: its fill and its surface read once."""
    insert = screen_insert.keyed_on(insert, frames[0])
    fill = screen_insert.static_fill(insert, frames[0], size)
    assert fill is not None
    surface = screen_insert.Surface.of(frames[0], [(x * size[0], y * size[1]) for x, y in insert.quad])
    state = screen_insert.Matting()
    return [
        screen_insert.compose(
            insert, picture, size, n, footage=frame, surface=surface, fill=fill, state=state, window_frame=n, evidence=evidence
        )
        for n, frame in enumerate(frames)
    ]


def _gate(layers: list, insert: InsertSource) -> tuple[list, dict]:
    """The findings and the report of an insert's composed *layers*, as the render reads its frame server's audits."""
    audits = {KEY: {"frames": {str(n): layer.audit.as_dict() for n, layer in enumerate(layers)}, "evidence": {}}}
    placed = {
        KEY: insert_checks.Placed(
            KEY,
            "s1",
            LABEL,
            0.0,
            len(layers) / FPS,
            light=insert.light,
            keyed=True,
            fit=insert.fit,
            key_settings=insert_checks.key_settings(insert.key),
            despill=insert.despill.as_dict(),
        )
    }
    found = [finding for finding, _proof in insert_checks.findings(audits, placed, fps=FPS)]
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return found, report


def _line(report: dict, found: list = ()) -> str:
    [line] = render_checks.insert_lines(render_checks.Checks(findings=list(found), inserts=[report]).as_dict())
    return line


def _at(layer, x: float, y: float) -> np.ndarray:
    x0, y0, _x1, _y1 = layer.warped.box
    return np.asarray(layer.warped.image, dtype=int)[int(y) - y0, int(x) - x0, :3]


# ── F1: a flat panel seen at an angle ─────────────────────────────────────


def test_f1_a_flat_panel_at_35_degrees_of_yaw_passes_its_angle_said_by_design():
    frames = [_panel(YAWED, seed=10 + n) for n in range(FRAMES)]
    # At save: flat, seen at an angle - never insert_flatness - said in a note.
    bezel = surface_refine.measure_bezel(frames[0], given=_quad(YAWED))
    assert bezel is not None and bezel.flatness.flat and bezel.flatness.error == 0.0
    assert bezel.flatness.angle == pytest.approx(0.25, abs=0.02) and bezel.flatness.angle_words().startswith("left side 2")
    measured = insert_bezels.Measured("s1", 0, ("clip", "c1"), "f", 0, 0.0, _quad(YAWED), bezel, "d")
    assert insert_bezels.findings([measured]) == []
    # Said in degrees: its yaw read through the lens the angle assumes, not the panel's own.
    note = insert_bezels.angle_note(measured)
    assert re.search(r"is seen at 3\d degrees on frame 0", note) and "by design" in note
    # Drawn: on its bezel to well under the gate, sharp, nothing found; its angle a fact of its report, by design.
    insert = _insert(YAWED)
    found, report = _gate(_drawn(insert, frames, _screenshot()), insert)
    assert found == [], [f.message for f in found]
    assert report["alignment_max"] is not None and report["alignment_max"] < surface_refine.ALIGNMENT_MAX_SHARE / 2
    assert report["sharpness"] >= screen_insert.MIN_SHARPNESS
    assert report["flatness"]["angle"] == pytest.approx(0.25, abs=0.02) and "curved" not in report["flatness"]
    assert re.search(r"by design \(a planar surface in perspective\): seen at 3\d degrees", _line(report))


# ── F2: a curved panel ────────────────────────────────────────────────────


def _cylinder(seed: int) -> np.ndarray:
    """A 1280x720 room with a green panel whose top and bottom edges bulge out 8% of its 600 px width (48 px)."""
    picture = _wall(seed, 1280, 720)
    top = [(340 + t * 600, 170 - 48 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(0, 1, 80)]
    bottom = [(340 + t * 600, 550 + 48 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(1, 0, 80)]
    ImageDraw.Draw(picture).polygon(top + bottom, fill=GREEN)
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0))).copy()


CHORDS = np.array([(340, 170), (940, 170), (940, 550), (340, 550)], dtype=float)


def test_f2_a_cylindrical_panel_bowing_8_percent_fails_on_its_flatness_alone():
    still = _cylinder(20)
    bezel = surface_refine.measure_bezel(still)
    assert bezel is not None and not bezel.flatness.flat and 0.065 < bezel.flatness.error < 0.095
    assert bezel.flatness.words().startswith(("its top edge bows", "its bottom edge bows"))
    measured = insert_bezels.Measured("s1", 0, ("clip", "c1"), "f", 0, 0.0, None, bezel, "d")
    [(scene_id, kind, words, shot)] = insert_bezels.findings([measured])
    assert (scene_id, kind, shot) == ("s1", "insert_flatness", "c1") and words.startswith("Regenerate the shot `c1` with a flat screen")
    # On the frames drawn: the one finding, the key left past its curved edges folded into it.
    frames = [_cylinder(21 + n) for n in range(FRAMES)]
    quad = tuple((float(x) / 1280, float(y) / 720) for x, y in CHORDS)
    insert = dataclasses.replace(_insert(CHORDS), quad=quad, box=tuple(screen_insert.grown_box(quad, 1280 / 720)))
    found, report = _gate(_drawn(insert, frames, _screenshot(size=(1200, 760)), (1280, 720)), insert)
    assert [f.kind for f in found] == [INSERT_FLATNESS], [f.message for f in found]
    [flat] = found
    assert flat.message.startswith("Regenerate the shot with a flat screen: insert on a curved surface at t=")
    assert INSERT_RESIDUAL_KEY in flat.data["follows"] and "key left past its edge follow from it" in flat.message
    assert report["flatness"]["bow"] > surface_refine.FLAT_MAX_SHARE
    assert "finding (insert_flatness): surface curved on" in _line(report, found)


# ── F3: the source's own green ────────────────────────────────────────────


def test_f3_a_screenshot_with_a_100_percent_green_ring_leaves_no_residual_key():
    frames = [_panel(SCREEN, seed=30 + n, lean=CYAN_LEAN) for n in range(FRAMES)]
    insert = _insert(SCREEN)
    found, report = _gate(_drawn(insert, frames, _screenshot(ring=True)), insert)
    assert found == [], [f.message for f in found]
    assert report["residual_key"] == 0.0 and report["halo"] is None
    # The ring is the source's own key hue, said: past the gate's share, it once failed every tolerance.
    assert report["source_key_hue"] > 3 * screen_insert.RESIDUAL_FAIL_SHARE
    assert "by design (its source's own colours): the source's own key hue on " in _line(report)


# ── F4: a hand crossing that ring ─────────────────────────────────────────


def test_f4_a_hand_crossing_the_ring_over_40_percent_of_the_quad_passes_its_colours_not_compared():
    frames = [_panel(SCREEN, seed=40)] + [_panel(SCREEN, seed=40 + n, hand=True) for n in range(1, FRAMES + 2)]
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot(ring=True))
    found, report = _gate(layers, insert)
    # A hand over 40 % of a legible insert hides what it is there to show: that alone is filed, never a colour.
    assert [f.kind for f in found] == [render_checks.INSERT_OCCLUDED], [f.message for f in found]
    found = [f for f in found if f.kind != render_checks.INSERT_OCCLUDED]
    assert found == [], [f.message for f in found]
    crossed = [layer.audit for layer in layers[1:]]
    assert all(0.33 < audit.foreground_share < 0.5 for audit in crossed), [audit.foreground_share for audit in crossed]
    # Too much of the insert covered to hold its colours to the source: not compared, said; nothing drawn over the hand.
    assert all(audit.colour_skipped and audit.black_lift is None for audit in crossed)
    assert all(not audit.covers_foreground for audit in crossed)
    assert layers[0].audit.colour_skipped is None and layers[0].audit.black_lift is not None
    assert report["foreground"]["skipped"] == len(crossed) and report["foreground"]["max"] > 0.33
    assert len(report["foreground"]["by_frame"]) == len(crossed)
    line = _line(report)
    assert f"colours not compared on {len(crossed)} frame(s) where it covers over 30% of the quad" in line
    # The hand's edge over the green ring is the source's, never a halo; the rest of it is measured.
    assert report["residual_key"] == 0.0
    assert report["halo"] is not None and report["halo"] <= screen_insert.MAX_HALO_SHARE and report["halo_edge"] > 100


# ── F5: a green running past the bezel ────────────────────────────────────


def test_f5_a_panel_whose_green_runs_1_percent_past_its_bezel_leaves_no_residual_and_fills_its_margin():
    frames = [_spilled(50 + n) for n in range(FRAMES)]
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    # Locked on the screen's own edge, never on the green's past the bezel.
    assert report["alignment_max"] <= 0.0025
    assert all(layer.audit.residual_key == 0.0 for layer in layers)
    # Within the residual margin, and in the green past the bezel, the bezel's colour on its side - never a grey of
    # the wall's light, never the green.
    reach = screen_insert.RESIDUAL_MARGIN * W
    (x0, y0), (_x1, y1) = SCREEN[0], SCREEN[2]
    for x, y in ((480, y0 - reach + 0.5), (480, y1 + reach - 0.5), (x0 - reach + 0.5, 265), (480, y0 - 0.008 * W)):
        pixel = _at(layers[-1], x, y)
        assert np.abs(pixel - BEZEL).max() <= 16, (x, y, pixel)
    assert report["despilled"]["share"] > 0


# ── F6: contain on a trapezoid ────────────────────────────────────────────


def test_f6_a_16_9_source_on_a_4_3_trapezoid_fills_its_bands_and_leaves_no_residual():
    frames = [_panel(TRAPEZOID, seed=60 + n) for n in range(FRAMES)]
    insert = _insert(TRAPEZOID, fit="contain")
    layers = _drawn(insert, frames, _screenshot(size=(1600, 900)))
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    audit = layers[-1].audit
    assert audit.band_fill == "source edge" and 0.15 < audit.band_share < 0.35
    assert report["residual_key"] == 0.0
    assert "by design (fit contain, key green): contain bands" in _line(report)


# ── F7: a camera moving by intent ─────────────────────────────────────────


CLIP = "00000000-0000-0000-0000-00000000d279"


def _dolly(**scene) -> tuple:
    from app.services.video_studio import quality, timeline
    from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

    raw = {"title": "Dolly", "freestyle": True, "scenes": [{"id": "d1", "type": "clip", "clip_id": CLIP, **scene}]}
    spec = parse_video_spec(validate_video_spec(raw))
    document = timeline.estimate_plan(spec, {}, {"d1": timeline.ClipMedia(4.0, 1920, 1080, 24.0, 96)}).document
    facts = quality.ContentFacts(shots={CLIP: quality.ShotFacts("Dolly in", "ready", width=1920, height=1080, camera="locked", motion_ssim=0.3)})
    return spec, document, facts, authored_spec(raw)


def _moved(found: list) -> list:
    return [f for f in found if f.kind == "camera_moved"]


def test_f7_a_clip_whose_camera_moves_by_intent_and_one_previewed_green_are_cleared():
    from app.services.video_studio import quality

    spec, document, facts, authored = _dolly()
    [moved] = _moved(quality.content_findings(spec, document, facts, authored))
    assert moved.severity in render_checks.BLOCKING_SEVERITIES and "set motion: intended on the scene" in moved.message
    # The author says the move is wanted: no finding.
    intended = _dolly(motion="intended")
    assert _moved(quality.content_findings(*intended)) == []
    # Its scene previewed green as it is now: cleared, with the render and the revision that cleared it.
    digests = {"d1": "digest-now"}
    passing = render_checks.Checks(findings=[]).as_dict()
    previews = render_checks.record_seen(None, digests, "render-7", checks=passing, revision=7)
    assert render_checks.cleared_shots(spec, render_checks.passed_scenes(previews, digests)) == {CLIP}
    facts.cleared = render_checks.cleared_by(spec, previews, digests)
    assert facts.cleared == {CLIP: {"render_id": "render-7", "revision": 7, "scenes": ["d1"]}}
    [cleared] = _moved(quality.content_findings(spec, document, facts, authored))
    assert cleared.severity not in render_checks.BLOCKING_SEVERITIES
    assert "cleared: the preview `render-7` of revision 7 of every scene showing it passed" in cleared.message
    # A preview of the scene as it was before an edit clears nothing.
    assert render_checks.cleared_by(spec, previews, {"d1": "digest-edited"}) == {}


# ── F8: a key setting draws other frames ──────────────────────────────────


def test_f8_a_key_at_tolerance_0_4_and_at_0_88_is_drawn_anew_and_both_pass():
    """F3 keyed at both tolerances: each passes, each read at its own tolerance, and the segments drawn at one are
    never served for the other - the key is a part of their digest. Where the key decides no pixel (F3's panel keys
    whole at both: its key sampled off it already holds its lean), the two drawings may be the same pixels: drawn
    again, never reused."""
    frames = [_panel(SCREEN, seed=80 + n, lean=CYAN_LEAN) for n in range(FRAMES)]
    narrow, wide = _insert(SCREEN, tolerance=0.4), _insert(SCREEN, tolerance=0.88)
    keyed = {}
    for insert in (narrow, wide):
        found, report = _gate(_drawn(insert, frames, _screenshot(ring=True)), insert)
        assert found == [], (insert.key.tolerance, [f.message for f in found])
        assert report["residual_key"] == 0.0 and report["source_key_hue"] > 0
        keyed[insert.key.tolerance] = screen_insert.keyed_on(insert, frames[0]).key
    assert keyed[0.4].matte_tolerance() < keyed[0.88].matte_tolerance()
    # The segments drawn at one are never the other's: the key is a part of the digest the frame server keeps them by.
    parts = {
        tolerance: compositor.drawing_inputs({"scenes": []}, None, (W, H), (0, FRAMES), compositor.CompositorAssets(inserts={KEY: insert}))
        for tolerance, insert in ((0.4, narrow), (0.88, wide))
    }
    assert parts[0.4][f"insert {KEY}"] != parts[0.88][f"insert {KEY}"]
    assert compositor.digest_of(parts[0.4]) != compositor.digest_of(parts[0.88])


def test_every_field_an_insert_is_drawn_by_moves_its_segments_digest():
    base = _insert(SCREEN)
    payload = {"src": KEY, "t0": 0.0, "t1": 2.0, "time_map": [[0.0, 0.0], [2.0, 2.0]]}

    def digest(insert: InsertSource, placed: dict = payload) -> str:
        document = {"scenes": [{"id": "s1", "inserts": [placed]}]}
        return compositor.drawing_inputs(document, None, (W, H), (0, 48), compositor.CompositorAssets(inserts={KEY: insert}))[f"insert {KEY}"]

    first = digest(base)
    changed = {
        "quad": dataclasses.replace(base, quad=_quad(SCREEN + 2.0)),
        "fit": dataclasses.replace(base, fit="contain"),
        "key": dataclasses.replace(base, key=KeySpec("green", 0.5, 3.0)),
        "light": dataclasses.replace(base, light=0.4),
        "glow": dataclasses.replace(base, glow=0.3),
        "source": dataclasses.replace(base, path="q.png"),
        # Followed frame by frame: its track's quads are what it is drawn into.
        "track": dataclasses.replace(base, track=SimpleNamespace(first=0, quads=[[list(p) for p in _quad(SCREEN)]], box=(0.2, 0.1, 0.8, 0.9))),
        # Its edge despill: its width, its strength, on or off.
        "despill width": dataclasses.replace(base, despill=screen_insert.Despill(width_px=4.0)),
        "despill off": dataclasses.replace(base, despill=screen_insert.Despill(edge=False)),
    }
    for name, insert in changed.items():
        assert digest(insert) != first, name
    trimmed = {**payload, "time_map": [[0.0, 1.5], [2.0, 3.5]]}
    assert digest(base, trimmed) != first, "trim"
    # A pane of a wall placed in another cell is another drawing.
    assert digest(base, {**payload, "pane": 1}) != first, "pane"


# ── The pieces ────────────────────────────────────────────────────────────


def _placed(**fields) -> dict:
    return {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0, **fields)}


def test_a_halo_needs_five_frames_past_its_share():
    def halo_on(count: int) -> list:
        frames = {str(n): {"halo": 0.08 if n < count else 0.01} for n in range(10)}
        return [f for f, _p in insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, _placed(), fps=FPS) if f.kind == INSERT_HALO]

    assert halo_on(insert_checks.HALO_MIN_FRAMES - 1) == []
    [halo] = halo_on(insert_checks.HALO_MIN_FRAMES)
    assert halo.data["frames"] == insert_checks.HALO_MIN_FRAMES


def test_a_colour_shift_no_light_moved_says_not_a_setting():
    settings = {"color": "green", "tolerance": 0.4, "feather": 3.0}
    frames = {str(n): {"black_lift": 0.055, "luma_shift": 0.02, "contrast": 0.7, "cast": 1.0, "coverage": 1.0} for n in range(6)}
    earlier = [
        {"key_settings": settings, "light": 0.08, "rest": "r", "measures": {"colour": round(0.055 / screen_insert.MAX_BLACK_LIFT, 3)}, "render_id": "p1"}
    ]
    placed = _placed(light=0.15, keyed=True, key_settings=settings, drawn="r")
    [finding] = [f for f, _p in insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, placed, fps=FPS, earlier={KEY: earlier})]
    assert "not a setting: source content - the source's own translucent pixels drawn over the footage" in finding.message
    assert "lower its light" not in finding.message and finding.data["unchanged"]["render_id"] == "p1"
    # Without that drawing, its light is the advice.
    [plain] = [f for f, _p in insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, placed, fps=FPS)]
    assert "lower its light (light: 0.15 now" in plain.message and "not a setting" not in plain.message


def test_a_halo_and_a_flicker_no_key_setting_moved_say_not_a_setting():
    settings, before = {"color": "green", "tolerance": 0.88, "feather": 9.0}, {"color": "green", "tolerance": 0.4, "feather": 3.0}
    halo = {str(n): {"halo": 0.081, "halo_hue": 300.0} for n in range(6)}
    flicker = {str(n): {"coverage": 1.0 if n % 2 else 0.95, "luma": 100.0, "source_luma": 100.0} for n in range(7)}
    earlier = [{"key_settings": before, "light": 0.15, "measures": {"halo": 0.081, "flicker": 5.0}, "render_id": "p2"}]
    placed = _placed(light=0.15, keyed=True, key_settings=settings)
    found = {f.kind: f for f, _p in insert_checks.findings({KEY: {"frames": {**halo}, "evidence": {}}}, placed, fps=FPS, earlier={KEY: earlier})}
    said = "not a setting: the foreground's own colour (magenta) - declare it with insert.despill.own_hues or accept"
    assert found[INSERT_HALO].message.count(said) == 1, found[INSERT_HALO].message
    assert found[INSERT_HALO].data["action"]["kind"] == insert_checks.DECLARE_OWN_HUES
    assert "widen the key's feather" not in found[INSERT_HALO].message
    found = {f.kind: f for f, _p in insert_checks.findings({KEY: {"frames": flicker, "evidence": {}}}, placed, fps=FPS, earlier={KEY: earlier})}
    flickered = found[render_checks.INSERT_FLICKER]
    assert "not a setting: foreground - the footage under the insert changes from frame to frame" in flickered.message
    assert "widen the key's tolerance" not in flickered.message


def test_a_curved_surface_on_the_frames_drawn_folds_the_findings_that_follow_from_it():
    curved = {
        "refined": [list(p) for p in _quad(SCREEN)],
        "size": [W, H],
        "edge_bows": [0.08, 0.0, 0.08, 0.0],
        "edge_inliers": [0.3, 1.0, 0.3, 1.0],
        "edge_residuals": [0.008, 0.0, 0.008, 0.0],
        "alignment": 0.012,
        "residual_key": 0.01,
        "residual_where": {"edge": {"top": 0.01}},
    }
    frames = {str(n): dict(curved) for n in range(8)}
    found = [f for f, _p in insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, _placed(), fps=FPS)]
    assert [f.kind for f in found] == [INSERT_FLATNESS]
    assert found[0].data["follows"] == [render_checks.INSERT_ALIGNMENT, INSERT_RESIDUAL_KEY]
    # The same bow most of its points deny is a content line of the screen: no flatness, the others as they are.
    denied = {str(n): {**curved, "edge_inliers": [0.95, 1.0, 0.95, 1.0], "edge_residuals": [0.001, 0.0, 0.001, 0.0]} for n in range(8)}
    kinds = {f.kind for f, _p in insert_checks.findings({KEY: {"frames": denied, "evidence": {}}}, _placed(), fps=FPS)}
    assert kinds == {render_checks.INSERT_ALIGNMENT, INSERT_RESIDUAL_KEY}
    # One weak signal alone never confirms a bow: a low inlier share with the points on one straight line - a
    # silhouette crossing a straight edge - is no curvature, nor is a bow under 5% whatever its points say.
    silhouette = {str(n): {**curved, "edge_inliers": [0.4, 1.0, 0.4, 1.0], "edge_residuals": [0.002, 0.0, 0.002, 0.0]} for n in range(8)}
    assert INSERT_FLATNESS not in {f.kind for f, _p in insert_checks.findings({KEY: {"frames": silhouette, "evidence": {}}}, _placed(), fps=FPS)}
    shallow = {str(n): {**curved, "edge_bows": [0.045, 0.0, 0.045, 0.0]} for n in range(8)}
    assert INSERT_FLATNESS not in {f.kind for f, _p in insert_checks.findings({KEY: {"frames": shallow, "evidence": {}}}, _placed(), fps=FPS)}
    # Four frames in a row are no surface: five are.
    brief = {str(n): dict(curved) if n < 4 else {**curved, "edge_bows": [0.0] * 4} for n in range(8)}
    assert INSERT_FLATNESS not in {f.kind for f, _p in insert_checks.findings({KEY: {"frames": brief, "evidence": {}}}, _placed(), fps=FPS)}


# ── F9 / F10: a round object in front of one edge ─────────────────────────


#: A round object - a ball - standing in front of the screen, over the lower part of its right edge and past its corner.
BALL = (205, 96, 40)


def _ball(picture: Image.Image, centre: tuple[float, float], radius: float) -> None:
    x, y = centre
    ImageDraw.Draw(picture).ellipse((x - radius, y - radius, x + radius, y + radius), fill=BALL)


def _occluded(seed: int, centre: tuple[float, float] = (895, 345), radius: float = 260) -> np.ndarray:
    """F1's flat keyed panel, square-on, a ball in front of the lower two-thirds of its right edge on every frame."""
    picture = _wall(seed)
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(SCREEN, 8.0)], fill=BEZEL)
    draw.polygon([tuple(p) for p in SCREEN], fill=GREEN)
    _ball(picture, centre, radius)
    return _blurred(picture, seed)


def _measured_save(frame: np.ndarray, quad_px: np.ndarray | None, size: tuple[int, int] = (W, H)) -> surface_refine.Bezel:
    """The save's measure of *frame* (``insert_bezels``): keyed as the insert keys, its quad the one given (``None``:
    left to the footage)."""
    key = KeySpec("green", 0.4, 3.0)
    given = _quad(quad_px, *size) if quad_px is not None else None
    return surface_refine.measure_bezel(frame, key.key(), key.matte_tolerance(), given=given, feather=key.feather)


def _shot_lint(monkeypatch, tmp_path, frame: np.ndarray) -> tuple[dict | None, str | None]:
    """The shot's own lint of a take whose first frame is *frame* (``clips.take_surface``): no insert's key
    reaches it, no quad given - ``(its record, why it is not flat)``."""
    from app.services.video_studio import clips, encoder

    async def extract_frame(_path, *, at, out_path, width=None):
        Image.fromarray(frame).save(out_path, format="PNG")

    monkeypatch.setattr(encoder, "extract_frame", extract_frame)
    return asyncio.run(clips.take_surface("take.mp4", str(tmp_path), {}))


def test_f9_a_flat_panel_with_a_ball_over_most_of_one_edge_passes_that_edge_mostly_covered_and_not_judged():
    frames = [_occluded(90 + n) for n in range(FRAMES)]
    # At save: flat, the right edge mostly behind the ball - said, its bow not judged.
    bezel = _measured_save(frames[0], SCREEN)
    assert bezel is not None and bezel.flatness.flat and bezel.flatness.covered() == ["right"]
    assert bezel.flatness.visible[1] < surface_refine.MIN_EDGE_VISIBLE and bezel.flatness.bows[1] is None
    measured = insert_bezels.Measured("s1", 0, ("clip", "c1"), "f", 0, 0.0, _quad(SCREEN), bezel, "d")
    assert insert_bezels.findings([measured]) == []
    assert re.search(r"has its right edge mostly covered \(\d+% visible\) on frame 0 .*: its bow is not judged there", insert_bezels.covered_note(measured))
    assert insert_bezels.verdicts([measured], 4)["insert/s1.0"] == [{"stage": "save", "id": "revision 4", "gates": {"flatness": True, "alignment": True}}]
    # Drawn: nothing found, the edge said as covered on every frame; the ball stays in front of the insert.
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    assert report["flatness"]["covered"] == {"right": FRAMES} and report["verdicts"]["flatness"] is True
    assert "right edge mostly covered by what stands in front on 8 frame(s): its bow not judged there" in _line(report)
    assert np.abs(_at(layers[-1], 655, 330) - BALL).max() <= 24, _at(layers[-1], 655, 330)
    # The save and the render read the same edges, the same way: one measure, two stages.
    audit = layers[0].audit
    assert audit.edge_visible == list(bezel.flatness.visible) and audit.edge_inliers == list(bezel.flatness.inliers)
    assert audit.edge_bows == list(bezel.flatness.bows)
    # Over half of the edge left visible, it is judged on that half - straight, so nothing found.
    half = [_occluded(95 + n, centre=(895, 405), radius=260) for n in range(FRAMES)]
    reading = _measured_save(half[0], SCREEN)
    assert surface_refine.MIN_EDGE_VISIBLE <= reading.flatness.visible[1] < 0.8 and reading.flatness.covered() == []
    assert reading.flatness.bows[1] is not None and reading.flatness.flat
    found, report = _gate(_drawn(insert, half, _screenshot()), insert)
    assert found == [] and "covered" not in report["flatness"], [f.message for f in found]


def _cylinder_occluded(seed: int) -> np.ndarray:
    """F2's cylindrical panel, the same ball in front of the lower part of its right edge."""
    picture = Image.fromarray(_cylinder(seed))
    _ball(picture, (1240, 420), 330)
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(0.6))).copy()


def test_f10_a_cylindrical_panel_with_the_same_ball_fails_its_flatness_on_the_edges_left_visible():
    still = _cylinder_occluded(20)
    # At save, its quad left to the footage (as F2): curved on its visible edges, the right one mostly covered.
    bezel = _measured_save(still, None, (1280, 720))
    assert bezel is not None and not bezel.flatness.flat and "right" in bezel.flatness.covered()
    assert bezel.flatness.words().startswith(("its top edge bows", "its bottom edge bows"))
    measured = insert_bezels.Measured("s1", 0, ("clip", "c1"), "f", 0, 0.0, None, bezel, "d")
    [(_scene, kind, _words, _shot)] = insert_bezels.findings([measured])
    assert kind == "insert_flatness"
    assert insert_bezels.verdicts([measured])["insert/s1.0"][0]["gates"]["flatness"] is False
    frames = [_cylinder_occluded(21 + n) for n in range(FRAMES)]
    quad = _quad(CHORDS, 1280, 720)
    insert = dataclasses.replace(_insert(CHORDS), quad=quad, box=tuple(screen_insert.grown_box(quad, 1280 / 720)))
    found, report = _gate(_drawn(insert, frames, _screenshot(size=(1200, 760)), (1280, 720)), insert)
    assert [f.kind for f in found] == [INSERT_FLATNESS], [f.message for f in found]
    [flat] = found
    # Said with the three readings that confirm it, on an edge fully visible, the covered edge named and not judged.
    assert re.search(r"its (top|bottom) edge bows \d+% of its length: 100% of it visible, \d+% of its points on one line", flat.message)
    assert "right edge mostly covered" in flat.message and flat.data["covered"] == ["right"]
    assert flat.data["action"]["kind"] == insert_checks.REGENERATE_FLAT and flat.data["gate"] == "flatness"
    assert flat.data["value"] > flat.data["threshold"] == surface_refine.FLAT_MAX_SHARE
    assert report["verdicts"]["flatness"] is False


# ── F11: one shot at save, at a 1080p preview and at a 2160p render ───────


F11_FRAMES = 5
#: A 900 x 420 screen on a 1920 x 1080 frame - flat behind its bezel, or a panel bulging 72 px (8%) top and bottom.
F11_SCREEN = np.array([(510, 330), (1410, 330), (1410, 750), (510, 750)], dtype=float)


def _f11_shot(seed: int, *, curved: bool) -> np.ndarray:
    picture = _wall(seed, 1920, 1080)
    draw = ImageDraw.Draw(picture)
    if curved:
        top = [(510 + t * 900, 330 - 72 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(0, 1, 120)]
        bottom = [(510 + t * 900, 750 + 72 * (1 - (2 * t - 1) ** 2)) for t in np.linspace(1, 0, 120)]
        draw.polygon(top + bottom, fill=GREEN)
    else:
        draw.polygon([tuple(p) for p in screen_insert._grown_px(F11_SCREEN, 12.0)], fill=BEZEL)
        draw.polygon([tuple(p) for p in F11_SCREEN], fill=GREEN)
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(1.2))).astype(np.float32)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 3.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _f11_drawn(frames: list[np.ndarray], size: tuple[int, int]) -> tuple[list, dict]:
    """The findings and report of the insert on *frames* drawn at *size* (the frames brought to it first, as the frame
    server scales the footage to the render's size)."""
    quad = tuple((float(x) / 1920, float(y) / 1080) for x, y in F11_SCREEN)
    insert = InsertSource(
        "p.png", "image", quad, key=KeySpec("green", 0.4, 3.0), footage=Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, 16 / 9)), label=LABEL,
    )
    scaled = [frame if frame.shape[1] == size[0] else np.asarray(Image.fromarray(frame).resize(size, Image.Resampling.BICUBIC)) for frame in frames]
    return _gate(_drawn(insert, scaled, _screenshot(size=(1800, 840)), size), insert)


@pytest.mark.parametrize("curved", [False, True])
def test_f11_one_shot_measured_at_save_at_a_1080p_preview_and_at_a_2160p_render_gives_one_verdict(curved, monkeypatch, tmp_path):
    frames = [_f11_shot(110 + n, curved=curved) for n in range(F11_FRAMES)]
    saved = _measured_save(frames[0], None, (1920, 1080))
    measured = insert_bezels.Measured("s1", 0, ("clip", "c1"), "f", 0, 0.0, None, saved, "d")
    saved_kinds = {kind for _scene, kind, _words, _shot in insert_bezels.findings([measured])}
    record, _unflat = _shot_lint(monkeypatch, tmp_path, frames[0])
    linted = surface_refine.Flatness.from_dict(record["flatness"]).flat
    preview_found, preview = _f11_drawn(frames, (1920, 1080))
    render_found, render = _f11_drawn(frames, (3840, 2160))
    # The flatness: one verdict at all four stages - the shot's own lint, the save, the preview and the
    # render - its thresholds shares of the edge and of the frame, the points' agreement read in proportion to the
    # frame.
    assert linted is saved.flatness.flat is preview["verdicts"]["flatness"] is render["verdicts"]["flatness"] is (not curved)
    # Every gate the preview and the render measured concludes the same, and so do their findings.
    assert preview["verdicts"] == render["verdicts"], (preview["verdicts"], render["verdicts"])
    assert [f.kind for f in preview_found] == [f.kind for f in render_found] == ([INSERT_FLATNESS] if curved else [])
    assert saved_kinds == ({"insert_flatness"} if curved else set())
    assert render["measured_at"] == [3840, 2160] and preview["measured_at"] == [1920, 1080]


# ── F12: text too small to read ───────────────────────────────────────────


def _dashboard(text_px: int) -> Image.Image:
    """A 1600 x 900 product UI: a 28 px heading, a table of *text_px* cells, a row of captions the same size."""
    from PIL import ImageFont

    picture = Image.new("RGB", (1600, 900), (246, 247, 249))
    draw = ImageDraw.Draw(picture)
    draw.text((60, 40), "Investigation overview", fill=(20, 22, 28), font=ImageFont.load_default(size=28))
    words = ["Detection rules updated", "Threat actors linked", "Indicators of compromise", "Last seen today"]
    for row in range(12):
        for col in range(3):
            draw.text((60 + col * 500, 120 + row * 48), words[(row + col) % 4], fill=(40, 44, 52), font=ImageFont.load_default(size=text_px))
    return picture


def test_f12_a_source_ui_whose_text_reads_4_px_on_the_1080p_frame_is_a_finding_naming_what_keeps_its_layout():
    from app.models.video_studio import JOB_READY
    from app.services.video_studio import insert_legibility, quality, timeline
    from app.services.video_studio.spec import parse_video_spec

    file_id, clip_id = "00000000-0000-0000-0000-00000000f294", "00000000-0000-0000-0000-00000000c294"
    text = insert_legibility.text_size(np.asarray(_dashboard(12)))
    assert text is not None and abs(text.px - 12) <= 2
    # Drawn 533 px wide on the 1080p frame, a third of its 1600 px: its 12 px text reads about 4 px.
    quad = [[0.2, 0.2], [0.2 + 533 / 1920, 0.2], [0.2 + 533 / 1920, 0.2 + 300 / 1080], [0.2, 0.2 + 300 / 1080]]
    raw = {
        "title": "Dashboard",
        "freestyle": True,
        "scenes": [{"id": "s1", "type": "clip", "clip_id": clip_id, "overlays": [{"type": "screen_insert", "quad": quad, "source": {"file_id": file_id}, "duration": 5}]}],
    }
    spec = parse_video_spec(raw)
    [item] = insert_legibility.judge(spec, {("file", file_id): text}, sources={("file", file_id): (1600, 900)}, footage={"s1": (1920, 1080)}, pixel_ratios={})
    assert 3.0 <= item.drawn_text <= 5.0 and item.illegible
    [(_scene, words, severity, _surface)] = insert_legibility.findings([item])
    # What brings it to 12 px and keeps its layout: a larger surface or a push - never a narrower capture of a
    # screenshot whose layout nobody probed.
    assert severity == "dense" and "of the 1080p frame's width (" in words and "push in on it with zoom" in words
    assert "viewport" not in words
    # A finding the render waits for, said with the save's other content findings.
    document = timeline.plan(spec, timeline.PlanMedia(clips={"s1": timeline.ClipMedia(8.0, 1920, 1080, 24.0, 192)}), fps=30, theme={}).document
    facts = quality.ContentFacts(shots={clip_id: quality.ShotFacts("Desk", JOB_READY, width=1920, height=1080)}, legibility=[item])
    [legible] = [f for f in quality.content_findings(spec, document, facts, None) if f.kind == "insert_legibility"]
    assert legible.severity in render_checks.BLOCKING_SEVERITIES and "push in on it with zoom" in legible.message


# ── F13: a regeneration that failed, its previous take playing ────────────


def test_f13_a_shot_whose_regeneration_failed_is_said_once_as_advice_and_never_holds_the_render():
    from app.services.video_studio import quality, timeline
    from app.services.video_studio.spec import authored_spec, parse_video_spec, validate_video_spec

    raw = {"title": "Retake", "freestyle": True, "scenes": [{"id": "r1", "type": "clip", "clip_id": CLIP}, {"id": "r2", "type": "clip", "clip_id": CLIP}]}
    spec = parse_video_spec(validate_video_spec(raw))
    media = {sid: timeline.ClipMedia(4.0, 1920, 1080, 24.0, 96) for sid in ("r1", "r2")}
    document = timeline.estimate_plan(spec, {}, media).document
    shot = quality.ShotFacts("Desk", "ready", width=1920, height=1080, regeneration_failed="the provider timed out.", playing="file-take-1")
    found = quality.content_findings(spec, document, quality.ContentFacts(shots={CLIP: shot}), authored_spec(raw))
    # Shown in two scenes, said once, naming the take that plays; advice only.
    [said] = [f for f in found if f.kind == "shot_state"]
    assert said.severity == "advisory" and said.scene_id == "r1" and said.clip_id == CLIP
    assert "last regeneration failed: the provider timed out, the previous take (file `file-take-1`) plays" in said.message
    assert not [f for f in found if f.severity in render_checks.BLOCKING_SEVERITIES]


# ── F14: a hand crossing a dark area of the UI ────────────────────────────


def _panel_hand(seed: int, *, hand: bool) -> np.ndarray:
    """F1's flat keyed panel; with *hand*, a hand rising into it through its bottom edge over a dark area of the UI it
    shows, on about a tenth of the quad."""
    picture = _wall(seed)
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(SCREEN, 8.0)], fill=BEZEL)
    draw.polygon([tuple(p) for p in SCREEN], fill=GREEN)
    if hand:
        skin = (196, 148, 118)
        draw.rounded_rectangle((520, 330, 610, 470), radius=26, fill=skin)
        for finger in range(2):
            draw.rounded_rectangle((526 + finger * 44, 296 + finger * 10, 560 + finger * 44, 350), radius=14, fill=skin)
    return _blurred(picture, seed)


def test_f14_a_hand_crossing_a_dark_ui_area_leaves_no_halo_and_its_colours_pass():
    frames = [_panel_hand(140, hand=False)] + [_panel_hand(140 + n, hand=True) for n in range(1, FRAMES + 2)]
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    crossed = [layer.audit for layer in layers[1:]]
    assert all(0.04 < audit.foreground_share < screen_insert.COLOUR_MAX_FOREGROUND for audit in crossed), [a.foreground_share for a in crossed]
    # Its edge measured over the UI's own dark pixels: no key hue on it.
    assert report["halo"] is not None and report["halo"] <= screen_insert.MAX_HALO_SHARE and report["halo_edge"] > 50
    # The colours compared clear of the hand, on every frame, and within their thresholds.
    assert all(audit.colour_skipped is None and audit.black_lift is not None for audit in crossed)
    assert report["colour"]["frames_failed"] == 0 and report["verdicts"]["colour_shift"] and report["verdicts"]["halo"]
    assert all(0.5 < audit.judged_share < 1.0 for audit in crossed)


# ── Save, preview and render: one measure ─────────────────────────


def test_a_render_finding_that_the_save_or_a_preview_of_the_same_shot_passed_disagrees_with_it_and_never_blocks():
    curved = {
        "refined": [list(p) for p in _quad(SCREEN)],
        "size": [W, H],
        "edge_bows": [0.08, 0.0, 0.08, 0.0],
        "edge_inliers": [0.3, 1.0, 0.3, 1.0],
        "edge_residuals": [0.008, 0.0, 0.008, 0.0],
        "edge_visible": [1.0, 1.0, 1.0, 1.0],
    }
    audits = {KEY: {"frames": {str(n): dict(curved) for n in range(8)}, "evidence": {}}}
    saved = {KEY: [{"stage": "save", "id": "revision 7", "gates": {"flatness": True, "alignment": True}}]}
    [(finding, _proof)] = insert_checks.findings(audits, _placed(), fps=FPS, passed=saved)
    assert finding.kind == INSERT_FLATNESS and finding.severity == render_checks.WARN
    assert finding.message.startswith("disagrees with save revision 7: Regenerate the shot with a flat screen")
    assert finding.data["disagrees"] == {
        "stage": "save",
        "id": "revision 7",
        "gate": "flatness",
        "cause": insert_checks.disagreement_cause(saved[KEY][0], FPS),
    }
    assert not render_checks.Checks(findings=[finding]).failed
    # A save that measured it curved too, or nothing measured before: the render's own verdict stands.
    for before in ({KEY: [{"stage": "save", "id": "revision 7", "gates": {"flatness": False}}]}, None):
        [(finding, _proof)] = insert_checks.findings(audits, _placed(), fps=FPS, passed=before)
        assert finding.severity == render_checks.FAIL and "disagrees" not in finding.data
    # A preview of the scene as it is now keeps what each gate concluded; a render of it is held to it.
    [report] = insert_checks.reports({KEY: {"frames": {str(n): {**curved, "edge_bows": [0.0] * 4} for n in range(8)}, "evidence": {}}}, _placed(), fps=FPS)
    assert report["verdicts"]["flatness"] is True
    previews = render_checks.record_seen(None, {"s1": "digest-now"}, "p9", checks=render_checks.Checks(inserts=[report]).as_dict(), revision=3, stage="preview")
    passed = render_checks.insert_verdicts(previews, {"s1": "digest-now"})
    assert passed[KEY] == [{"stage": "preview", "id": "p9", "revision": 3, "gates": report["verdicts"]}]
    [(finding, _proof)] = insert_checks.findings(audits, _placed(), fps=FPS, passed=passed)
    assert finding.severity == render_checks.WARN and finding.message.startswith("disagrees with preview p9: ")
    # A preview of the scene before an edit is no evidence: another shot, another spec.
    assert render_checks.insert_verdicts(previews, {"s1": "digest-edited"}) == {}
    # A pane of a wall is held to what its overlay's save measured.
    assert insert_checks.passed_records(saved, f"{KEY}.2") == saved[KEY]


# ── The shot's own lint, one verdict with the save and the render ──


#: The audit of a frame whose top and bottom edges bow 8%, their points confirming it.
CURVED_AUDIT = {
    "refined": [list(p) for p in _quad(SCREEN)],
    "size": [W, H],
    "edge_bows": [0.08, 0.0, 0.08, 0.0],
    "edge_inliers": [0.3, 1.0, 0.3, 1.0],
    "edge_residuals": [0.008, 0.0, 0.008, 0.0],
    "edge_visible": [1.0, 1.0, 1.0, 1.0],
}


def test_f9_and_f10_the_shots_own_lint_gives_the_verdict_of_the_save_and_the_render(monkeypatch, tmp_path):
    # F9: a flat panel, the ball over most of its right edge - the take is not failed, as the save and the render pass.
    record, unflat = _shot_lint(monkeypatch, tmp_path, _occluded(90))
    assert unflat is None and surface_refine.Flatness.from_dict(record["flatness"]).flat
    assert _measured_save(_occluded(90), SCREEN).flatness.flat
    # F10: the cylindrical panel with the same ball - failed on its curved top and bottom edges, as at save and render;
    # the ball's silhouette on the right edge confirms nothing.
    record, unflat = _shot_lint(monkeypatch, tmp_path, _cylinder_occluded(20))
    shape = surface_refine.Flatness.from_dict(record["flatness"])
    assert not shape.flat and shape.words().startswith(("its top edge bows", "its bottom edge bows")), shape.edges()
    assert shape.confirmed(0) and shape.confirmed(2) and not shape.confirmed(1)
    assert unflat.startswith("Regenerate the shot with a flat screen: the surface is curved")
    assert not _measured_save(_cylinder_occluded(20), None, (1280, 720)).flatness.flat


def test_a_bow_is_never_confirmed_by_one_reading_alone_not_even_on_a_record_older_than_its_points():
    sides = SCREEN.tolist()
    bowed = (0.08, 0.0, 0.08, 0.0)
    # A bow whose points' agreement was not read (an older record): not judged.
    legacy = surface_refine.flatness(sides, bowed)
    assert legacy.flat and not legacy.confirmed(0) and legacy.error == 0.0
    assert surface_refine.Flatness.from_dict({"sides": [360.0, 270.0, 360.0, 270.0], "bows": list(bowed)}).flat
    # Its points off one line, but on one straight line through them all: a silhouette, not a curve.
    assert surface_refine.flatness(sides, bowed, (0.3, 1.0, 0.3, 1.0)).flat
    assert surface_refine.flatness(sides, bowed, None, (0.008, 0.0, 0.008, 0.0)).flat
    # The three together confirm it.
    curved = surface_refine.flatness(sides, bowed, (0.3, 1.0, 0.3, 1.0), (0.008, 0.0, 0.008, 0.0))
    assert not curved.flat and curved.confirmed(0) and curved.confirmed(2)


def _surface_record(curved: bool, take: str) -> dict:
    """A shot's own lint of its take *take* (``frame_qc.SURFACE``), its screen *curved* or flat."""
    shape = (
        surface_refine.flatness(SCREEN, (0.08, 0.0, 0.08, 0.0), (0.3, 1.0, 0.3, 1.0), (0.008, 0.0, 0.008, 0.0))
        if curved
        else surface_refine.flatness(SCREEN, (0.0,) * 4, (1.0,) * 4, (0.0,) * 4)
    )
    return {"quad": [list(p) for p in _quad(SCREEN)], "flatness": shape.as_dict(), "take_file_id": take}


def test_a_render_finding_the_shots_own_lint_passed_on_the_take_that_plays_disagrees_with_it_and_never_blocks():
    from app.services.video_studio import frame_qc

    params = {frame_qc.QC_KEY: {frame_qc.SURFACE: _surface_record(False, "file-1")}}
    assert insert_bezels.shot_verdict(params, "file-1") is True
    assert insert_bezels.shot_verdict({frame_qc.QC_KEY: {frame_qc.SURFACE: _surface_record(True, "file-1")}}, "file-1") is False
    # Another file plays - a take a later step drew over, an earlier take - or nothing was measured: no evidence.
    assert insert_bezels.shot_verdict(params, "file-2") is None
    assert insert_bezels.shot_verdict({}, "file-1") is None

    class Rows:
        async def get(self, _model, ident):
            return SimpleNamespace(params=params) if str(ident) == CLIP else None

    playing = insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 0, 0.0, None, None, "d")
    passed = asyncio.run(insert_bezels.shot_verdicts(Rows(), [playing]))
    assert passed == {KEY: [{"stage": "save", "id": f"shot {CLIP}", "gates": {"flatness": True}}]}
    audits = {KEY: {"frames": {str(n): dict(CURVED_AUDIT) for n in range(8)}, "evidence": {}}}
    [(finding, _proof)] = insert_checks.findings(audits, _placed(), fps=FPS, passed=passed)
    assert finding.kind == INSERT_FLATNESS and finding.severity == render_checks.WARN
    assert finding.message.startswith(f"disagrees with save shot {CLIP}: ")
    assert not render_checks.Checks(findings=[finding]).failed
    # The take measured no longer plays, or an upload plays: the render's own verdict stands.
    replaced = dataclasses.replace(playing, file_id="file-2")
    upload = dataclasses.replace(playing, footage=("file", "file-1"))
    assert asyncio.run(insert_bezels.shot_verdicts(Rows(), [replaced, upload])) == {}


def test_a_quality_report_carries_the_version_of_the_check_the_studio_keys_it_by():
    from app.services.video_studio import quality

    report = quality.QualityReport(scene_count=2, revision=100)
    assert report.as_dict()["version"] == quality.QUALITY_REPORT_VERSION
    assert quality.QualityReport.from_dict(report.as_dict()) == report
    assert f":v{quality.QUALITY_REPORT_VERSION}:" in quality._cache_key(uuid.uuid4(), uuid.uuid4(), 100)


# ── The key's spill on the edge of what stands in front ────────────


#: A matte key panel a video model painted off the primary: a green leaning cyan, which the clamp's channel rules read
#: as a two-channel key (``screens._key_channels``), lifting its red instead of limiting its green.
MATTE_KEY = (40, 200, 130)
#: The subject's fur, and the share of the key its edge holds in the footage: the panel's light on its own edge.
FUR = (150, 100, 70)
EDGE_TINT = 0.2
#: The strands along the subject's outline: ``(how many around it, how long, where they start)``.
STRANDS = ((23, 3.0, 0.4), (37, 2.2, 1.9), (51, 3.6, 4.1), (67, 1.8, 2.6))


def _fur(shift: float = 0.0, scale: float = 1.0) -> np.ndarray:
    """A round furry subject rising into the panel through its bottom edge, its outline jagged with strands."""
    width, height = round(W * scale), round(H * scale)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    cx, cy, radius = (480.0 + shift) * scale, 430.0 * scale, 110.0 * scale
    angle = np.arctan2(yy - cy, xx - cx)
    strands = sum(length * np.sin(count * angle + phase) for count, length, phase in STRANDS) * scale
    return np.hypot(xx - cx, yy - cy) <= radius + strands


def _furry(seed: int, *, subject: bool, shift: float = 0.0, scale: float = 1.0, tint_px: float = 4.0) -> np.ndarray:
    """F1's flat panel painted :data:`MATTE_KEY`; with *subject*, the furry subject in front of it, its outer *tint_px*
    pixels holding up to :data:`EDGE_TINT` of the key."""
    width, height = round(W * scale), round(H * scale)
    screen = SCREEN * scale
    picture = _wall(seed, width, height)
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(screen, 8.0 * scale)], fill=BEZEL)
    draw.polygon([tuple(p) for p in screen], fill=MATTE_KEY)
    pixels = np.asarray(picture).astype(np.float32)
    if subject:
        mask = _fur(shift, scale)
        reach = tint_px * scale
        tint = (np.clip((reach + 1.0 - ndimage.distance_transform_edt(mask)) / reach, 0.0, 1.0) * EDGE_TINT)[..., None]
        colour = np.array(FUR, dtype=np.float32) * (1.0 - tint) + np.array(MATTE_KEY, dtype=np.float32) * tint
        pixels = np.where(mask[..., None], colour, pixels)
    out = np.asarray(Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(scale))).astype(np.float32)
    out += np.random.default_rng(seed + 1000).normal(0.0, 3.0, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def _furry_frames(**kwargs) -> list[np.ndarray]:
    """The panel alone on the insert's first frame, then the subject in front of it, moving 2 px a frame."""
    return [_furry(150, subject=False, **kwargs)] + [_furry(150 + n, subject=True, shift=2.0 * n, **kwargs) for n in range(1, FRAMES + 2)]


def _shown(layer, frame: np.ndarray) -> np.ndarray:
    """*layer* as the page shows it over *frame*, on the layer's box."""
    x0, y0, x1, y1 = layer.warped.box
    drawn = np.asarray(layer.warped.image, dtype=np.float32)
    alpha = drawn[..., 3:4] / 255.0
    return drawn[..., :3] * alpha + frame[y0:y1, x0:x1].astype(np.float32) * (1.0 - alpha)


def _lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0-255) as CIE L*a*b* (D65): what a delta E is measured in."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _interior(layer, n: int) -> np.ndarray:
    """The subject's interior on frame *n* - 8 px in from its outline, inside the screen - over *layer*'s box."""
    x0, y0, x1, y1 = layer.warped.box
    within = screen_insert._coverage(SCREEN, layer.warped.box) > 0.5
    return ndimage.binary_erosion(_fur(2.0 * n), iterations=8)[y0:y1, x0:x1] & within


# ── F15: a furry subject whose edge holds the panel's colour ──────────────


def test_f15_a_furry_subject_whose_edge_holds_a_fifth_of_a_matte_key_leaves_no_halo_and_keeps_its_interior():
    frames = _furry_frames()
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    crossed = [layer.audit for layer in layers[1:]]
    # The key's colour taken off the subject's edge: under 2% of that edge on every frame.
    assert all(audit.halo is not None and audit.halo < 0.02 for audit in crossed), [a.halo for a in crossed]
    assert report["halo"] < 0.02 and report["halo_edge"] > 200
    # What the edge despill corrected, frame by frame, and in the insert's report.
    assert all(audit.edge_spill is not None and audit.edge_spill > 0.05 for audit in crossed), [a.edge_spill for a in crossed]
    spill = report["edge_spill"]
    assert (spill["edge"], spill["width_px"], spill["strength"]) == (True, screen_insert.EDGE_SPILL_PX, 1.0)
    assert spill["frames"] == spill["frames_read"] == len(crossed) and spill["max"] >= spill["mean"] > 0.05
    lines = [m.line() for m in insert_checks.classify(report)]
    assert any(line.startswith("by design (key green): edge despill (width 2 px) corrected up to ") for line in lines), lines
    # The subject's interior keeps the footage's own colours: delta E under 1.
    for n, layer in enumerate(layers[1:], start=1):
        interior = _interior(layer, n)
        x0, y0, x1, y1 = layer.warped.box
        delta = np.linalg.norm(_lab(_shown(layer, frames[n])[interior]) - _lab(frames[n][y0:y1, x0:x1][interior]), axis=-1)
        assert interior.sum() > 2000 and delta.max() < 1.0, (n, float(delta.max()))


def test_a_wider_edge_despill_leaves_less_of_the_key_on_the_edge():
    """What the halo finding advises moves the halo: the despill's width."""
    frames = _furry_frames()
    halos = {}
    for width in (0.0, screen_insert.EDGE_SPILL_PX):
        insert = dataclasses.replace(_insert(SCREEN), despill=screen_insert.Despill(width_px=width))
        _found, report = _gate(_drawn(insert, frames, _screenshot()), insert)
        halos[width] = report["halo"]
    assert halos[0.0] > 0.005 and halos[screen_insert.EDGE_SPILL_PX] < halos[0.0] / 2, halos


# ── F16: F15 with its edge despill off ────────────────────────────────────


def test_f16_f15_with_its_edge_despill_off_fails_its_halo_saying_the_edge_spill_and_turning_it_on():
    frames = _furry_frames()
    insert = dataclasses.replace(_insert(SCREEN), despill=screen_insert.Despill(edge=False))
    layers = _drawn(insert, frames, _screenshot(), evidence=True)
    found, report = _gate(layers, insert)
    [halo] = [f for f in found if f.kind == INSERT_HALO]
    assert report["halo"] > screen_insert.MAX_HALO_SHARE
    said = re.search(r": edge spill (\d+\.\d)% with the edge despill off \(despill\.edge: false\) on the foreground's edge \(\d+ px long\)", halo.message)
    assert said is not None, halo.message
    assert halo.data["action"]["kind"] == insert_checks.WIDEN_DESPILL
    assert halo.data["action"]["text"] == "turn the edge despill on (despill: {edge: true}) before generating the shot again"
    assert halo.data["despill"] == {"edge": False, "width_px": screen_insert.EDGE_SPILL_PX, "strength": 1.0}
    # Nothing corrected, said so.
    assert all(layer.audit.edge_spill is None for layer in layers) and report["edge_spill"]["edge"] is False
    assert any("despill.edge false" in m.line() for m in insert_checks.classify(report, {INSERT_HALO}))
    # Its worst stretch kept at 100%: the subject's edge, the key's hue on it (the same stretch despilled holds none).
    worst = max(range(1, len(layers)), key=lambda n: layers[n].audit.halo)
    crop = layers[worst].halo_crop
    assert crop is not None and min(crop.size) >= screen_insert.HALO_CROP_MIN_PX
    key = screen_insert.keyed_on(insert, frames[0]).key.key()
    assert screen_insert._hue_outliers(screen_insert._hsv(np.asarray(crop, dtype=np.float32)), key, complement=False).mean() > 0.01
    # The edge treatment never reaches the interior: F15 draws it as this does.
    treated = _drawn(_insert(SCREEN), frames, _screenshot())
    for n in range(1, len(frames)):
        interior = _interior(layers[n], n)
        assert np.array_equal(np.asarray(layers[n].warped.image)[interior], np.asarray(treated[n].warped.image)[interior]), n


# ── F17: the insert's light on the subject's edge ─────────────────────────


def test_f17_the_insert_s_light_wraps_onto_the_subject_s_edge_on_a_2_px_band_only():
    # A subject in front of an insert of one colour, on a 1080p frame.
    yy, xx = np.mgrid[0:300, 0:300]
    front = np.hypot(xx - 150.0, yy - 190.0) <= 90.0
    colours = np.empty((300, 300, 3), dtype=np.float32)
    colours[...] = FUR
    behind = np.empty_like(colours)
    behind[...] = (30.0, 90.0, 200.0)
    wrapped = screen_insert.light_wrap(colours, behind, front, np.ones(front.shape, dtype=bool), 1.0)
    depth = ndimage.distance_transform_edt(front)
    changed = np.abs(wrapped - colours).max(axis=-1) > 0.5
    assert changed.any()
    assert not (changed & ~front).any(), "nothing past the subject's edge"
    assert not (changed & (depth > screen_insert.LIGHT_WRAP_PX)).any(), "nothing deeper than 2 px"
    took = (wrapped - colours)[..., 2] / (behind - colours)[..., 2]
    edge, second = front & (depth == 1.0), front & (depth > 1.0) & (depth <= screen_insert.LIGHT_WRAP_PX)
    # Its edge pixels take 20-30% of the insert behind them, the next ring less.
    assert took[edge].min() >= 0.2 and took[edge].max() <= 0.3
    assert (took[second] > 0.0).all() and (took[second] < took[edge].min()).all()


def test_f17_a_1080p_drawing_wraps_the_insert_s_light_on_the_subject_s_edge_alone(monkeypatch):
    # The subject standing still in front of the panel, drawn at 1080p with the wrap and without it.
    frames = [_furry(150, subject=False, scale=2.0)] + [_furry(151 + n, subject=True, scale=2.0) for n in range(4)]
    size = (2 * W, 2 * H)
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    wrapped = _drawn(insert, frames, _screenshot(), size)
    monkeypatch.setattr(screen_insert, "LIGHT_WRAP_SHARE", 0.0)
    plain = _drawn(insert, frames, _screenshot(), size)
    # What stands in front as the drawing reads it: the occlusion against the fill's first frame, the lower median of
    # the last three frames' (``screen_insert.Matting``) with the frame's own reading, inside the quad drawn.
    fill = screen_insert.static_fill(insert, frames[0], size)
    quad_px = [(x * size[0], y * size[1]) for x, y in insert.quad]
    surface = screen_insert.Surface.of(frames[0], quad_px)
    inside = screen_insert._coverage(fill.drawn_px, fill.box) > 0.5
    last = len(frames) - 1
    window = [surface.visible(frames[n], quad_px, fill.box, insert.key.feather, full=False)[0] for n in range(last - 2, last + 1)]
    front = np.minimum(np.sort(np.stack(window), axis=0)[1], window[-1]) < 0.5
    x0, y0, x1, y1 = fill.box
    # Less the pockets of the key the frame's own matte reads inside it: the insert is drawn there.
    raw = screens.key_matte(frames[last][y0:y1, x0:x1].astype(np.float32), insert.key.key(), insert.key.matte_tolerance())
    reach = max(1, int(np.ceil(insert.key.feather * size[1] / 1080.0)))
    _visible, front = screen_insert.key_out_pockets(np.zeros(front.shape, dtype=np.float32), front, inside, raw, reach)
    depth = ndimage.distance_transform_edt(~(inside & ~front))
    band = front & inside & (depth <= screen_insert.LIGHT_WRAP_PX)
    moved = np.abs(np.asarray(wrapped[last].warped.image, dtype=np.float32) - np.asarray(plain[last].warped.image, dtype=np.float32))
    moved = moved[..., :3].max(axis=-1) > 1.0
    # On the subject's edge, 2 px deep from the insert at most, all along it, and nowhere else - but where the frame's own
    # key reads the key whole: the key's residue the occlusion holds in front, drawn as the insert itself.
    assert moved.sum() > 300 and not (moved & ~band).any(), (int(moved.sum()), int((moved & ~band).sum()))
    keyed = raw > 0.9
    edge = band & (depth == 1.0) & ~keyed
    assert (moved & edge).sum() > 0.8 * edge.sum(), (int((moved & edge).sum()), int(edge.sum()))


# ── The pieces of the key's spill ─────────────────────────────────────────


SETTINGS = {"color": "green", "tolerance": 0.4, "feather": 3.0}


def _halo_frames(value: float = 0.075) -> dict:
    frames = {str(n): {"halo": value, "halo_edge": 350} for n in range(6)}
    evidence = {"halo": [{"badness": value, "frame": 2, "path": "f.jpg"}], "halo_crop": [{"badness": value, "frame": 2, "path": "h.png"}]}
    return {KEY: {"frames": frames, "evidence": evidence}}


def _despilled(**fields) -> dict:
    return {**screen_insert.Despill().as_dict(), **fields}


def test_a_halo_after_the_despill_says_its_width_and_its_crop_and_advises_widening_it_up_to_its_bound():
    placed = _placed(keyed=True, key_settings=SETTINGS, despill=_despilled())
    [(halo, proof)] = insert_checks.findings(_halo_frames(), placed, fps=FPS)
    assert proof == "f.jpg"
    assert "Scene s1: its screen insert 0: edge spill 7.5% after despill width 2 px on the foreground's edge (350 px long)" in halo.message
    assert "its worst stretch kept at 100%" in halo.message
    assert halo.data["action"] == {
        "kind": insert_checks.WIDEN_DESPILL,
        "text": "raise despill.width_px (2 now; 4 reaches farther into the edge) before generating the shot again",
    }
    # The report keeps the crop, stored as a file of the render and named on its halo's line.
    [report] = insert_checks.reports(_halo_frames(), placed, fps=FPS)
    assert report["halo_crop"] == {"frame": 2, "at": round(2 / FPS, 3), "path": "h.png"}
    report["halo_crop"]["file_id"] = "file-halo"
    lines = [m.line() for m in insert_checks.classify(report, {INSERT_HALO})]
    assert "finding (insert_halo): halo 7.5% of the foreground's edge (350 px long), after despill width 2 px; its worst stretch at 100%: file `file-halo`" in lines
    assert "file-halo" in render_checks.insert_crop_ids(render_checks.Checks(inserts=[report]).as_dict())
    # A weakened despill is raised to its full strength; at its widest, the despill is no longer the advice.
    weak = _placed(keyed=True, key_settings=SETTINGS, despill=_despilled(strength=0.5))
    [(halo, _proof)] = insert_checks.findings(_halo_frames(), weak, fps=FPS)
    assert "after despill width 2 px at strength 0.5" in halo.message and "its strength to 1" in halo.data["action"]["text"]
    widest = _placed(keyed=True, key_settings=SETTINGS, despill=_despilled(width_px=16.0))
    [(halo, _proof)] = insert_checks.findings(_halo_frames(), widest, fps=FPS)
    assert halo.data["action"]["kind"] == insert_checks.REGENERATE_SHOT and "despill.width_px" not in halo.data["action"]["text"]


def test_a_drawing_at_another_despill_that_left_the_same_halo_says_not_a_setting():
    placed = _placed(keyed=True, key_settings=SETTINGS, light=0.15, despill=_despilled())
    wider = [{"key_settings": SETTINGS, "light": 0.15, "despill": _despilled(width_px=6.0), "measures": {"halo": 0.075}, "render_id": "p3"}]
    [(halo, _proof)] = insert_checks.findings(_halo_frames(), placed, fps=FPS, earlier={KEY: wider})
    # No setting moves it: the foreground's own colour, which the author declares or accepts.
    assert halo.data["action"]["kind"] == insert_checks.DECLARE_OWN_HUES
    assert "not a setting: the foreground's own colour - declare it with insert.despill.own_hues or accept" in halo.message
    assert "despill width 2 strength 1" in halo.message and "despill width 6 strength 1" in halo.message
    # A drawing at the same key, light and despill is no evidence - nor one kept before the despill, drawn at its default.
    for same in ({**wider[0], "despill": _despilled()}, {key: value for key, value in wider[0].items() if key != "despill"}):
        [(halo, _proof)] = insert_checks.findings(_halo_frames(), placed, fps=FPS, earlier={KEY: [same]})
        assert halo.data["action"]["kind"] == insert_checks.WIDEN_DESPILL and "unchanged" not in halo.data
    # The scene's drawing keeps the despill it was measured at.
    [report] = insert_checks.reports(_halo_frames(), placed, fps=FPS)
    checks = render_checks.Checks(inserts=[report]).as_dict()
    assert render_checks.insert_measures(checks, "s1")[KEY]["despill"] == _despilled()


def test_the_save_says_the_edge_spill_a_preview_will_read_before_any_preview():
    frame = _furry(152, subject=True, shift=4.0)
    key = KeySpec("green", 0.4, 3.0)
    measured = surface_refine.measure_bezel(frame, key.key(), key.matte_tolerance(), given=_quad(SCREEN), feather=key.feather)
    assert measured is not None and measured.spill is not None
    assert measured.spill.share > insert_bezels.SPILL_NOTE_SHARE and measured.spill.after is not None and measured.spill.after < 0.02
    # Kept with the measure and read back.
    assert surface_refine.Bezel.from_dict(json.loads(json.dumps(measured.as_dict()))).spill == measured.spill
    item = insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 0, 0.0, _quad(SCREEN), measured, "d")
    note = insert_bezels.spill_note(item)
    assert note is not None and note.startswith("Scene s1: what stands in front of the surface under its screen insert 0 carries the key's hue on ")
    assert re.search(r"of its edge \(\d+ px\) on frame 0 \(0\.00 s\) of its footage: expected edge spill; the edge despill \(width 2 px\) leaves \d\.\d% of it\.$", note), note
    assert note in insert_bezels.resolve({"scenes": [{"id": "s1", "overlays": [{"type": "screen_insert"}]}]}, [item]).notes
    # Its despill off, the save says the halo gate reads that edge as it is.
    off = surface_refine.measure_bezel(
        frame, key.key(), key.matte_tolerance(), given=_quad(SCREEN), feather=key.feather, despill=screen_insert.Despill(edge=False)
    )
    assert off.spill.after is None and off.spill.share == measured.spill.share
    assert "which the edge despill is off for (despill.edge: false)" in insert_bezels.spill_note(dataclasses.replace(item, bezel=off))
    # Nothing in front: nothing to say; no key: no matte to read it on.
    clear = surface_refine.measure_bezel(_furry(150, subject=False), key.key(), key.matte_tolerance(), given=_quad(SCREEN), feather=key.feather)
    assert clear.spill is None and insert_bezels.spill_note(dataclasses.replace(item, bezel=clear)) is None
    assert surface_refine.measure_bezel(frame, given=_quad(SCREEN)).spill is None
    # The measure is kept by the despill it read.
    assert insert_bezels._digest("f", 0.0, None, key) != insert_bezels._digest("f", 0.0, None, key, _despilled(width_px=6.0))


def test_the_despill_travels_from_the_spec_to_every_drawing_of_the_insert():
    from app.services.video_studio.spec import MAX_INSERT_DESPILL_WIDTH, Overlay

    base = {"type": "screen_insert", "source": {"asset": "logo"}, "quad": [list(p) for p in _quad(SCREEN)], "key": {"color": "green"}}
    overlay = Overlay.model_validate(base)
    assert overlay.despill is None and timeline.insert_despill(overlay) is None
    assert screen_insert.Despill.of(timeline.insert_despill(overlay)) == screen_insert.Despill()
    named = Overlay.model_validate({**base, "despill": {"edge": False, "width_px": 6}})
    assert timeline.insert_despill(named) == {"edge": False, "width_px": 6.0, "strength": 1.0}
    for wrong in ({"width_px": MAX_INSERT_DESPILL_WIDTH + 1}, {"strength": 1.5}, {"edge": True, "radius": 2}):
        with pytest.raises(ValueError):
            Overlay.model_validate({**base, "despill": wrong})
    # A text overlay takes none: the field is dropped with the insert's other fields.
    assert "despill" not in Overlay.model_validate({"type": "callout", "text": "x", "despill": {"edge": False}}).model_dump(exclude_none=True)
    # A range's worker reads it back with the insert.
    insert = dataclasses.replace(_insert(SCREEN), despill=screen_insert.Despill(edge=False, width_px=6.0))
    published = json.loads(json.dumps(render_ranges._manifest(compositor.CompositorAssets(inserts={KEY: insert}), lambda path: path), default=str))
    assert screen_insert.Despill.of(published["inserts"][KEY]["despill"]) == insert.despill
    # The editor's build is drawn again when it changes, and keeps its digest while it names none.
    build = insert_builds.InsertBuild(KEY, "s1", "f", "s", "image", _quad(SCREEN), 0.0, 2.0, ())
    assert build.digest() != dataclasses.replace(build, despill=timeline.insert_despill(named)).digest()
    assert build.digest() == dataclasses.replace(build, despill=timeline.insert_despill(overlay)).digest()
    # What its findings are held to apart from its key, light and despill.
    part = SimpleNamespace(overlay=overlay, kind="asset", ref="logo", quad=_quad(SCREEN), pane=None)
    assert insert_checks.rest_digest(part) == insert_checks.rest_digest(SimpleNamespace(**{**vars(part), "overlay": named}))


# ── The subject's own colour on its edge ───────────────────────────


#: Colours a subject holds of its own, each within the halo's 30 degrees of the key's complement (the panel's green is
#: at hue 122): a muzzle's pink, a jacket's magenta; and a grey head.
MUZZLE = (226, 140, 196)
JACKET = (170, 90, 160)
HEAD = (196, 192, 186)
#: The dark UI the insert shows: what the audience sees where the key took the panel.
UI_DARK = (22, 26, 34)


def _hue(rgb: tuple) -> float:
    import colorsys

    return colorsys.rgb_to_hsv(*(channel / 255.0 for channel in rgb))[0] * 360.0


def _panel_only(seed: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    picture = _wall(seed)
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(SCREEN, 8.0)], fill=BEZEL)
    draw.polygon([tuple(p) for p in SCREEN], fill=GREEN)
    return picture, draw


def _muzzle(seed: int, *, tip: float | None) -> np.ndarray:
    """The flat green panel; with *tip*, a grey head with a pink muzzle coming in from the right, past the panel's
    bezel, the muzzle's tip alone *tip* pixels into the panel - smeared 9 px across by its motion, as a shutter does."""
    picture, _draw = _panel_only(seed)
    pixels = np.asarray(picture).astype(np.float32)
    if tip is not None:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        cx = float(SCREEN[1][0]) - tip + 30.0
        muzzle = ((xx - cx) / 30.0) ** 2 + ((yy - 265.0) / 26.0) ** 2 <= 1.0
        head = ((xx - cx - 95.0) / 80.0) ** 2 + ((yy - 255.0) / 70.0) ** 2 <= 1.0
        layer = np.zeros(pixels.shape, dtype=np.float32)
        layer[head], layer[muzzle] = HEAD, MUZZLE
        alpha = (head | muzzle).astype(np.float32)
        smeared = ndimage.uniform_filter1d(alpha, 9, axis=1)
        layer = ndimage.uniform_filter1d(layer * alpha[..., None], 9, axis=1) / np.maximum(smeared, 1e-6)[..., None]
        pixels = pixels * (1.0 - smeared[..., None]) + layer * smeared[..., None]
    return _blurred(Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)), seed)


def _muzzle_frames() -> list[np.ndarray]:
    """The panel alone for ten frames, then the muzzle's tip 5 to 12 px into it over eight: present under half the clip,
    so the clip's own background is the panel and the wall."""
    return [_muzzle(180 + n, tip=None) for n in range(10)] + [_muzzle(190 + n, tip=5.0 + n) for n in range(8)]


def _keyed_frames(insert: InsertSource, frames: list[np.ndarray], picture: Image.Image) -> list:
    """Each frame keyed frame by frame, as the frame server draws a tracked insert: what stands in front is read inside
    the quad alone - of a muzzle crossing it, its tip."""
    insert = screen_insert.keyed_on(insert, frames[0])
    state = screen_insert.Matting()
    return [screen_insert.compose(insert, picture, (W, H), n, footage=frame, state=state, window_frame=n) for n, frame in enumerate(frames)]


def _read_subject(insert: InsertSource, frames: list[np.ndarray]) -> screen_insert.Subject | None:
    """What the save reads of what stands in front over the clip (``compositor.read_subject``), on these frames."""
    keyed = insert.key
    readings = [(frame, insert.quad, insert.box) for frame in frames]
    return screen_insert.subject_of(readings, keyed.key(), keyed.matte_tolerance(), feather=keyed.feather, still=insert.track is None)


# ── F18: a pink muzzle crossing the panel by its tip ──────────────────────


def test_f18_a_pink_nosed_subject_crossing_the_panel_by_its_tip_passes_its_halo_its_own_colour_said():
    frames = _muzzle_frames()
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    key = insert.key.key()
    # At save, over the clip: the muzzle read whole past the quad, where its tip alone crosses it; its pink its own.
    subject = _read_subject(insert, frames)
    assert subject is not None and subject.frames >= 5
    share, hue = subject.near(_hue(key.rgb) + 180.0)
    assert share >= screen_insert.OWN_HUE_MIN_SHARE and subject.owns(np.array([hue]))[0], (share, hue)
    assert subject.near(_hue(key.rgb))[0] < screen_insert.OWN_HUE_MIN_SHARE, "the panel's green is never the subject's"
    item = insert_bezels.Measured("s1", 0, ("clip", CLIP), "file-1", 0, 0.0, _quad(SCREEN), None, "d", subject, round(_hue(key.rgb), 1))
    note = insert_bezels.subject_note(item)
    assert note is not None and " - the key's complement - on " in note and "the halo gate never counts" in note, note
    assert note in insert_bezels.resolve({"scenes": [{"id": "s1", "overlays": [{"type": "screen_insert"}]}]}, [item]).notes
    # Drawn frame by frame - its tip all the quad holds of it, the frames a per-pixel share of its interior failed at
    # 7-26% -: no halo, the muzzle's pink left out of the edge, said frame by frame and in the report.
    insert = dataclasses.replace(insert, subject=subject)
    layers = _keyed_frames(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert INSERT_HALO not in {f.kind for f in found}, [f.message for f in found]
    crossed = [layer.audit for layer in layers[10:] if layer.audit.halo_own is not None]
    assert len(crossed) >= insert_checks.MIN_FAILING_FRAMES, [layer.audit.halo_edge for layer in layers]
    assert all(audit.halo is None or audit.halo <= screen_insert.MAX_HALO_SHARE for audit in crossed), [a.halo for a in crossed]
    assert max(audit.halo_own for audit in crossed) > 0.2 and all(not audit.halo_clamped for audit in crossed)
    assert report["halo_parts"]["own"] > 0.2 and report["halo_parts"]["clamped"] == 0.0
    # Its pink is never despilled off the edge as the key's spill: the tip shows it as the footage does.
    last, frame = layers[-1], frames[-1]
    x0, y0, x1, y1 = last.warped.box
    inside = screen_insert._coverage(SCREEN, last.warped.box) > 0.5
    pink = inside & screen_insert._hue_outliers(screen_insert._hsv(frame[y0:y1, x0:x1].astype(np.float32)), key, complement=True)
    kept = screen_insert._hue_outliers(screen_insert._hsv(_shown(last, frame)), key, complement=True)
    assert pink.sum() > 100 and (kept & pink).sum() >= 0.9 * pink.sum(), (int(pink.sum()), int((kept & pink).sum()))


def test_f18_the_muzzle_s_tip_alone_is_its_own_colour_without_the_clip_and_the_tracked_reading_is_the_quad_s():
    """Rule 3 holds on a frame read alone: the footage's complement, which the clamp never touched, is never spill; and
    a tracked insert's reading (no background to tell the subject past the quad) is the quad's foreground alone."""
    frames = _muzzle_frames()
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    found, report = _gate(_keyed_frames(insert, frames, _screenshot()), insert)
    assert INSERT_HALO not in {f.kind for f in found} and report["halo_parts"]["own"] > 0.2, report.get("halo_parts")
    moving = _read_subject(dataclasses.replace(insert, track=SimpleNamespace()), frames)
    still = _read_subject(insert, frames)
    assert moving is None or moving.pixels < still.pixels


# ── F19: a grey subject with a real green fringe ──────────────────────────


#: A fringe the matte keeps in front: the grey subject's own edge holding 35% of the panel's green.
FRINGE = tuple(round(0.65 * 128 + 0.35 * channel) for channel in GREEN)


def _fringed(seed: int, *, rise: float | None) -> np.ndarray:
    """The flat green panel; with *rise*, a grey subject rising *rise* pixels into it through its bottom edge, its
    outer 3 px holding the panel's green in the footage."""
    picture, _draw = _panel_only(seed)
    pixels = np.asarray(picture).astype(np.float32)
    if rise is not None:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        subject = np.hypot(xx - 480.0, yy - (float(SCREEN[2][1]) - rise + 110.0)) <= 110.0
        fringe = subject & ~ndimage.binary_erosion(subject, iterations=3)
        pixels[subject], pixels[fringe] = (128.0, 128.0, 128.0), FRINGE
    return _blurred(Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8)), seed)


def test_f19_a_grey_subject_with_a_real_3_px_green_fringe_fails_its_halo():
    frames = [_fringed(200 + n, rise=None) for n in range(6)] + [_fringed(206 + n, rise=50.0 + 6.0 * n) for n in range(6)]
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    key, tolerance, scale = insert.key.key(), insert.key.matte_tolerance(), H / 1080.0
    # Over the clip, the subject is grey: its edge - where the fringe is - is never read as its colour.
    subject = _read_subject(insert, frames)
    assert subject is not None and subject.near(_hue(key.rgb))[0] < screen_insert.OWN_HUE_MIN_SHARE
    # The gate on each frame as the audience would see that fringe: the panel the insert's dark UI, the subject as shot.
    box = screen_insert.bbox(list(insert.box), W, H)
    x0, y0, x1, y1 = box
    inside = screen_insert._coverage(SCREEN, box) > 0.5
    reach = max(2.0, screen_insert.EDGE_DESPILL_PX * scale + 2.0 * insert.key.feather * scale)
    audits = {}
    for n, frame in enumerate(frames[6:]):
        region = frame[y0:y1, x0:x1].astype(np.float32)
        matte = screens.key_matte(region, key, tolerance)
        front = screen_insert._foreground(matte < 0.5, inside, scale)
        seen = region.copy()
        seen[inside & ~front] = UI_DARK
        edge = screen_insert.edge_of(region, front, key, reach, subject=subject)
        read = screen_insert._halo_share(screen_insert._hsv(seen), front, inside, key, scale, own=edge)
        audits[str(n)] = {"halo": read.share, "halo_own": read.own, "halo_edge": read.edge, "halo_clamped": read.clamped, "halo_hue": read.hue}
    assert all(audit["halo"] > screen_insert.MAX_HALO_SHARE for audit in audits.values()), audits
    assert all(not audit["halo_clamped"] for audit in audits.values()), "nothing the clamp touched: the footage's own fringe"
    [(halo, _proof)] = insert_checks.findings({KEY: {"frames": audits, "evidence": {}}}, _placed(keyed=True, key_settings=SETTINGS), fps=FPS)
    assert halo.kind == INSERT_HALO and halo.data["parts"]["hue"] == "green", halo.data


# ── F20 / F21: a magenta jacket, then an inverting clamp on its edge ──────


def _jacket(seed: int, *, rise: float | None) -> np.ndarray:
    """The flat green panel; with *rise*, a subject in a magenta jacket rising *rise* pixels into it through its bottom
    edge."""
    picture, draw = _panel_only(seed)
    if rise is not None:
        draw.rounded_rectangle((400, float(SCREEN[2][1]) - rise, 560, H + 40), radius=50, fill=JACKET)
    return _blurred(picture, seed)


def _jacket_frames() -> list[np.ndarray]:
    return [_jacket(220, rise=None)] + [_jacket(221 + n, rise=60.0 + n) for n in range(8)]


def test_f20_a_subject_in_a_magenta_jacket_with_no_clamp_on_it_passes_its_halo():
    frames = _jacket_frames()
    insert = _insert(SCREEN)
    layers = _drawn(insert, frames, _screenshot())
    found, report = _gate(layers, insert)
    assert found == [], [f.message for f in found]
    # Past the matte's window (three frames), what stands in front is read where the jacket is.
    crossed = [layer.audit for layer in layers[3:] if layer.audit.halo_own is not None]
    assert len(crossed) >= insert_checks.MIN_FAILING_FRAMES
    # The jacket's magenta on its edge is the footage's, untouched by the clamp: left out, said, never counted. Read on
    # the frame's own edge, never a frame behind it, the edge holds it on fewer of its pixels.
    assert all(audit.halo <= screen_insert.MAX_HALO_SHARE and not audit.halo_clamped for audit in crossed), [(a.halo, a.halo_clamped) for a in crossed]
    assert any(audit.halo_own > 0.0 for audit in crossed) and report["halo_parts"]["own"] > 0.0


def _inverting_clamp(monkeypatch, insert: InsertSource) -> None:
    """The compositor's clamp made to invert the key on a 31 px stretch of the jacket's top edge: there, on the jacket's
    pixels within 3 px of the panel, the key's chroma taken out past neutral - the magenta an inverting despill leaves."""
    clamp = screens.despill_clamp
    x0, y0, x1, y1 = screen_insert.bbox(list(insert.box), W, H)
    shift = 0.6 * (np.array(GREEN, dtype=np.float32) - np.mean(GREEN))

    def inverting(rgb, key, where=None):
        out = clamp(rgb, key, where)
        pixels = np.asarray(rgb, dtype=np.float32)
        if pixels.shape[:2] != (y1 - y0, x1 - x0):
            return out
        keyed = screens.key_matte(pixels, key, insert.key.matte_tolerance()) >= 0.5
        stretch = np.zeros(keyed.shape, dtype=bool)
        stretch[150 - y0 : 390 - y0, 465 - x0 : 496 - x0] = True
        inverted = ndimage.binary_dilation(keyed, iterations=3) & ~keyed & stretch
        out = np.array(out, dtype=np.float32, copy=True)
        out[inverted] = np.clip(pixels[inverted] - shift, 0.0, 255.0)
        return out

    monkeypatch.setattr(screens, "despill_clamp", inverting)


#: What an inverting clamp leaves of the key on the jacket's edge: past neutral, far more saturated than the jacket.
INVERTED = (215, 20, 205)


def test_f21_an_inverting_clamp_on_a_tenth_of_the_jacket_s_edge_fails_its_halo_naming_the_clamp():
    frames = _jacket_frames()
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    key, tolerance, scale = insert.key.key(), insert.key.matte_tolerance(), H / 1080.0
    box = screen_insert.bbox(list(insert.box), W, H)
    x0, y0, x1, y1 = box
    inside = screen_insert._coverage(SCREEN, box) > 0.5
    reach = max(2.0, screen_insert.EDGE_DESPILL_PX * scale + 2.0 * insert.key.feather * scale)
    subject = _read_subject(insert, frames)
    # Each frame as the audience would see it: the clamp's magenta on a stretch of the jacket's edge a tenth of it long,
    # the panel the insert's dark UI elsewhere, the jacket as shot.
    audits = {}
    for n, frame in enumerate(frames[1:]):
        region = frame[y0:y1, x0:x1].astype(np.float32)
        front = screen_insert._foreground(screens.key_matte(region, key, tolerance) < 0.5, inside, scale)
        band, boundary, _reach = screen_insert._halo_band(front, inside, scale)
        stretch = np.zeros(front.shape, dtype=bool)
        stretch[150 - y0 : 390 - y0, 465 - x0 : 503 - x0] = True
        assert 0.08 < (boundary & stretch).sum() / boundary.sum() < 0.12, "a tenth of its edge"
        drawn = screens.despill_clamp(region, key, inside)
        drawn[band & stretch] = INVERTED
        seen = drawn.copy()
        seen[inside & ~front & ~(band & stretch)] = UI_DARK
        edge = screen_insert.edge_of(region, front, key, reach, drawn=drawn, subject=subject)
        read = screen_insert._halo_share(screen_insert._hsv(seen), front, inside, key, scale, own=edge)
        audits[str(n)] = {"halo": read.share, "halo_own": read.own, "halo_edge": read.edge, "halo_clamped": read.clamped, "halo_hue": read.hue}
    # Counted where the clamp touched the edge alone - never on the jacket's own magenta beside it, which it holds
    # over its clip and which is left out.
    assert subject is not None and subject.owns(np.array([_hue(JACKET)]))[0]
    assert all(audit["halo"] > screen_insert.MAX_HALO_SHARE and audit["halo_clamped"] == audit["halo"] for audit in audits.values()), audits
    assert all(audit["halo_own"] > 0.2 for audit in audits.values())
    placed = _placed(keyed=True, key_settings=SETTINGS, despill=_despilled(edge=False))
    [(halo, _proof)] = insert_checks.findings({KEY: {"frames": audits, "evidence": {}}}, placed, fps=FPS)
    assert halo.kind == INSERT_HALO and "of the edge is counted where the compositor's clamp touched it" in halo.message, halo.message
    assert halo.data["parts"]["clamped"] > screen_insert.MAX_HALO_SHARE and halo.data["parts"]["hue"] == "magenta"
    assert halo.data["action"]["kind"] == insert_checks.WIDEN_DESPILL and "despill: {edge: true}" in halo.data["action"]["text"]
    [report] = insert_checks.reports({KEY: {"frames": audits, "evidence": {}}}, placed, fps=FPS)
    lines = [m.line() for m in insert_checks.classify(report, {INSERT_HALO})]
    assert any("counted where the clamp touched it" in line and "the subject's own colour, not counted" in line for line in lines), lines


def test_f21_the_compositor_s_own_clamp_mask_reaches_the_gate_and_the_edge_despill_takes_what_it_made(monkeypatch):
    frames = _jacket_frames()
    insert = screen_insert.keyed_on(_insert(SCREEN), frames[0])
    _inverting_clamp(monkeypatch, insert)
    # The edge despill off: what the clamp made reaches the frame, counted as the clamp's and nothing else.
    layers = _drawn(dataclasses.replace(insert, despill=screen_insert.Despill(edge=False)), frames, _screenshot())
    counted = [layer.audit for layer in layers[1:] if layer.audit.halo]
    assert len(counted) >= insert_checks.MIN_FAILING_FRAMES and all(audit.halo_clamped == audit.halo for audit in counted), [
        (layer.audit.halo, layer.audit.halo_clamped) for layer in layers
    ]
    assert all(screen_insert.hue_name(audit.halo_hue) == "magenta" for audit in counted)
    # On: the edge despill recolours what the clamp made from the jacket beside it, and the gate agrees.
    treated = _drawn(insert, frames, _screenshot())
    assert all(not layer.audit.halo for layer in treated), [layer.audit.halo for layer in treated]


# ── The pieces of the subject's edge colour ───────────────────────────────


def test_own_hues_travel_from_the_spec_to_every_drawing_and_cover_their_span_of_the_wheel():
    from app.services.video_studio.spec import Overlay

    base = {"type": "screen_insert", "source": {"asset": "logo"}, "quad": [list(p) for p in _quad(SCREEN)], "key": {"color": "green"}}
    overlay = Overlay.model_validate({**base, "despill": {"own_hues": ["Magenta", "#E8A0B0", "magenta"]}})
    assert overlay.despill.own_hues == ["magenta", "#e8a0b0"]
    assert timeline.insert_despill(overlay) == {"edge": True, "width_px": 2.0, "strength": 1.0, "own_hues": ["magenta", "#e8a0b0"]}
    for wrong in (["pinkish"], ["#e8a0b"], ["red"] * 9):
        with pytest.raises(ValueError):
            Overlay.model_validate({**base, "despill": {"own_hues": wrong}})
    despill = screen_insert.Despill.of(timeline.insert_despill(overlay))
    assert despill.own_hues == ("magenta", "#e8a0b0") and despill.as_dict()["own_hues"] == ["magenta", "#e8a0b0"]
    # A name covers its span of the wheel, a colour its hue 15 degrees either side - across 0 too.
    covered = screen_insert.in_spans(np.array([300.0, 344.0, 346.0, 340.0, 355.0, 20.0]), despill.spans())
    assert covered.tolist() == [True, True, True, True, True, False]
    assert screen_insert.in_spans(np.array([5.0, 350.0, 30.0]), screen_insert.own_hue_spans(["#ff0010"])).tolist() == [True, True, False]
    # No own hue: the despill's record, and every digest, as before.
    assert "own_hues" not in screen_insert.Despill().as_dict() and screen_insert.Despill.of({"edge": True}) == screen_insert.Despill()
    # The insert's subject and its own hues reach a range's worker and move the segments' digest.
    subject = screen_insert.Subject((0.0,) * 30 + (0.05,) + (0.0,) * 5, 4000, 6)
    insert = dataclasses.replace(_insert(SCREEN), despill=despill, subject=subject)
    published = json.loads(json.dumps(render_ranges._manifest(compositor.CompositorAssets(inserts={KEY: insert}), lambda path: path), default=str))
    assert screen_insert.Despill.of(published["inserts"][KEY]["despill"]) == despill
    assert screen_insert.Subject.from_dict(published["inserts"][KEY]["subject"]) == subject
    plain = compositor._insert_inputs(_insert(SCREEN), compositor.CompositorAssets(), [])
    assert compositor._insert_inputs(insert, compositor.CompositorAssets(), []) != plain and "subject" not in plain


def test_a_declared_own_hue_is_never_counted_even_where_the_clamp_made_it():
    """The author's word is the last on what the subject is: a hue it declares is its own on the edge, the clamp's
    included; what it holds over its clip is not, there (F21)."""
    yy, xx = np.mgrid[0:120, 0:160]
    front = (yy > 40) & (xx > 20) & (xx < 140)
    region = np.empty((120, 160, 3), dtype=np.float32)
    region[...] = GREEN
    region[front] = JACKET
    ring = front & ~ndimage.binary_erosion(front, iterations=2)
    drawn = region.copy()
    drawn[ring] = (215, 20, 205)
    key = screens.Key("green", GREEN)
    seen = drawn.copy()
    seen[~front] = UI_DARK
    hsv, inside = screen_insert._hsv(seen), np.ones(front.shape, dtype=bool)
    jacket = screen_insert.Subject(tuple(0.4 if index == 30 else 0.0 for index in range(36)), 9000, 8)
    held = screen_insert.edge_of(region, front, key, 3.0, drawn=drawn, subject=jacket)
    assert screen_insert._halo_share(hsv, front, inside, key, 1.0, own=held).share > screen_insert.MAX_HALO_SHARE
    declared = screen_insert.edge_of(region, front, key, 3.0, drawn=drawn, declared=screen_insert.own_hue_spans(["magenta"]))
    read = screen_insert._halo_share(hsv, front, inside, key, 1.0, own=declared)
    assert read.share is not None and read.share <= screen_insert.MAX_HALO_SHARE and read.own > 0.2


@pytest.mark.skipif(not encoder.encoder_available(), reason="ffmpeg is not installed")
def test_the_subject_is_read_off_the_footage_file_as_off_its_frames(tmp_path):
    """``compositor.read_subject`` - what the save and the render call - decodes the clip a few frames at a time, at
    most ``SUBJECT_WIDTH`` wide, and reads what the frames themselves give: the muzzle's pink its own."""
    frames = _muzzle_frames()
    workdir = tmp_path / "frames"
    workdir.mkdir()
    names = []
    for index, frame in enumerate(frames):
        Image.fromarray(frame).save(workdir / f"f{index:03d}.png")
        names.append((index / FPS, f"f{index:03d}.png"))
    path = str(tmp_path / "shot.mp4")
    asyncio.run(encoder.frames_to_mezzanine(names, end_time=len(frames) / FPS, out_path=path, fps=FPS, crf=12, preset="ultrafast", workdir=str(workdir)))
    insert = dataclasses.replace(_insert(SCREEN), footage=Footage(path, "video", FPS), first_frame=0)
    read = asyncio.run(compositor.read_subject(insert, len(frames) - 1))
    key = screen_insert.keyed_on(_insert(SCREEN), frames[0]).key.key()
    assert read is not None and read.frames >= 5
    assert read.near(_hue(key.rgb) + 180.0)[0] >= screen_insert.OWN_HUE_MIN_SHARE and read.near(_hue(key.rgb))[0] < screen_insert.OWN_HUE_MIN_SHARE
    # The save's own call: the key read on the frame it comes in on, its hue kept with the reading.
    subject, key_hue = asyncio.run(insert_bezels._subject(path, frames[0], _quad(SCREEN), KeySpec("green", 0.4, 3.0), 0.0, (len(frames) - 1) / FPS, FPS, label=LABEL))
    assert subject is not None and abs(key_hue - _hue(key.rgb)) < 2.0
    # Nothing keyed, or no video: nothing read.
    assert asyncio.run(compositor.read_subject(dataclasses.replace(insert, key=None), len(frames) - 1)) is None
    assert asyncio.run(compositor.read_subject(dataclasses.replace(insert, footage=Footage(path, "image")), len(frames) - 1)) is None


def test_the_save_reads_the_subject_once_by_its_clip_and_the_render_takes_it_for_every_pane():
    subject = screen_insert.Subject((0.0,) * 36, 1000, 4)
    item = insert_bezels.Measured("s1", 2, ("clip", CLIP), "file-1", 0, 0.0, None, None, "d", subject, 122.0)
    assert insert_bezels.subjects([item, dataclasses.replace(item, index=3, subject=None)]) == {"insert/s1.2": subject}
    # Kept by the clip's span it was read over: a longer insert reads it again.
    key = KeySpec("green", 0.4, 3.0)
    assert insert_bezels._digest("f", 0.0, None, key, until=2.0) != insert_bezels._digest("f", 0.0, None, key, until=3.0)
    # Nothing of the key's hue nor its complement: nothing to say.
    assert insert_bezels.subject_note(item) is None
    from app.services.video_studio import render

    payload, footage = {"t0": 0.0, "t1": 1.0}, {"time_map": []}
    for key_name in ("insert/s1.2", "insert/s1.2.1"):
        found = asyncio.run(render._subject_of(key_name, _insert(SCREEN), payload, footage, {"insert/s1.2": subject}))
        assert found is subject
