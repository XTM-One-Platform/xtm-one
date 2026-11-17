"""Screen inserts drawn on their bezel, sharp, keyed clean, and the gate that fails what the audience would see.

On synthetic footage, no provider, no browser: a static bezel, a bezel the
camera pushes in on, a green surface under uneven tinted light, a person
crossing the surface. Each is composed frame by frame through
``screen_insert.compose`` - the render's own function, with the matte's
window read ahead as the frame server reads it - and read back as the
render's findings and per-insert report (``insert_checks``). What is proven:

* the bezel lock (``surface_refine``) snaps a quad a few pixels off to the
  bezel and measures the given quad's error against it, the inner edge of
  the bezel winning over the outer one, a person across an edge moving it
  nowhere;
* the four fixtures pass: alignment, residual key, flicker and sharpness
  under their thresholds and reported, the person kept in front without a
  halo, the surface under uneven light one surface;
* a deliberately wrong quad fails the render (``insert_alignment``, the
  surface running past a quad too small ``insert_residual_key``), and a
  hard-edged matte - binary, undespilled - fails it (``insert_halo``,
  ``insert_residual_key``);
* the despill clamps and never inverts; the warp is supersampled and sharp;
  the box a matted insert covers is the quad grown, the same for the page;
  the crops of the insert region are picked, kept and named in the answer.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import compositor, insert_checks, render_checks, screen_insert, screens, surface_refine
from app.services.video_studio.insert_tracking import InsertTrack
from app.services.video_studio.tracking import luma

W, H = 960, 540
GREEN = (32, 255, 30)
#: The surface's true corners (pixels): a monitor seen a little from the side.
TRUE = np.array([(285, 112), (675, 128), (668, 420), (292, 405)], dtype=float)
FRAMES = 8
FPS = 30.0
KEY = "insert/demo.0"


def _room(
    surface: np.ndarray,
    *,
    seed: int,
    light: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None,
    person: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """A textured room, a dark bezel, a flat green surface; lit unevenly, a person in front, the encoder's noise."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = surface.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (surface - centre) * 1.05], fill=(18, 20, 24))
    draw.polygon([tuple(p) for p in surface], fill=GREEN)
    frame = np.asarray(picture).astype(np.float32)
    if light is not None:
        mask_image = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask_image).polygon([tuple(p) for p in surface], fill=255)
        mask = np.asarray(mask_image) > 0
        t = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
        gain = (1 - t) * np.array(light[0], dtype=np.float32) + t * np.array(light[1], dtype=np.float32)
        frame[mask] = (frame * gain)[mask]
    if person is not None:
        x, y, r = person
        who = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8))
        ImageDraw.Draw(who).ellipse((x - r, y - r, x + r, y + 3 * r), fill=(70, 50, 40))
        frame = np.asarray(who.filter(ImageFilter.GaussianBlur(1.2))).astype(np.float32)
    frame += rng.normal(0.0, 4.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _ui(width: int = 1310, height: int = 1000) -> Image.Image:
    """A dashboard: lines of text on a dark ground, denser than the quad it is drawn into, and of its proportions
    (a ``cover`` fit that cut its content would be a finding of its own, ``insert_cropped``)."""
    picture = Image.new("RGB", (width, height), (22, 26, 34))
    draw = ImageDraw.Draw(picture)
    rng = np.random.default_rng(5)
    for row in range(40, height - 40, 28):
        x = 60
        while x < width - 80:
            w = int(rng.integers(20, 120))
            draw.rectangle((x, row, x + w, row + 10), fill=(200, 205, 215))
            x += w + 14
    return picture


def _insert(quad_px: np.ndarray, *, keyed: bool = True, track: InsertTrack | None = None) -> screen_insert.InsertSource:
    quad = tuple((float(x) / W, float(y) / H) for x, y in quad_px)
    return screen_insert.InsertSource(
        "p.png",
        "image",
        quad,
        key=screen_insert.KeySpec("green") if keyed else None,
        footage=screen_insert.Footage("f", "video", FPS, "clip/demo"),
        track=track,
        label="Scene demo: its screen insert 0",
    )


def _composed(frames: list[np.ndarray], insert: screen_insert.InsertSource, *, state: bool = True) -> tuple[dict, list, dict]:
    """Every frame composed as the frame server composes it; the audits, the findings and the insert's report."""
    picture = _ui()
    surface = None
    if insert.key is not None:
        insert = screen_insert.keyed_on(insert, frames[0])
    else:
        quad, _within = insert.quads(0)
        surface = screen_insert.Surface.of(frames[0], [(x * W, y * H) for x, y in quad])
    matting = screen_insert.Matting() if state else None
    audits: dict = {"frames": {}, "evidence": {}}
    layers = []
    for n, frame in enumerate(frames):
        ahead = [(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)]
        layer = screen_insert.compose(
            insert, picture, (W, H), n, footage=frame, surface=surface, state=matting, footage_ahead=ahead, evidence=True
        )
        assert layer is not None
        layers.append(layer)
        audits["frames"][str(n)] = layer.audit.as_dict()
    placed = {KEY: insert_checks.Placed(KEY, "demo", insert.label, 0.0, len(frames) / FPS)}
    found = insert_checks.findings({KEY: audits}, placed, fps=FPS)
    [report] = insert_checks.reports({KEY: audits}, placed, fps=FPS)
    return audits, found, report, layers


# ── The bezel lock ────────────────────────────────────────────────────────


def test_the_bezel_lock_snaps_a_quad_off_its_bezel_and_measures_the_given_quad_s_error():
    field = luma(_room(TRUE, seed=1))
    band = max(surface_refine.MIN_BAND_PX, surface_refine.BAND * W)
    for offset in ((0, 0), (5, -3), (9, 7), (-14, 10)):
        found = surface_refine.refine(field, TRUE + np.array(offset, dtype=float), band)
        assert found.found and found.edges == 4, offset
        # Snapped to the true edges within a pixel, whichever side of the inner bezel edge the seed sat on.
        assert np.linalg.norm(found.quad - TRUE, axis=1).max() < 1.0, offset
        assert found.seed_error == pytest.approx(float(np.hypot(*offset)), abs=1.2)
    # The lock, frame after frame: the error reported is the GIVEN quad's, whatever seed found the bezel.
    refiner = surface_refine.Refiner()
    for _ in range(4):
        step = refiner.step(field, TRUE + np.array([6.0, 5.0]), W)
        assert step.found and step.seed_error == pytest.approx(np.hypot(6.0, 5.0) / W, abs=1.2 / W)
        assert np.linalg.norm(step.quad - TRUE, axis=1).max() < 1.0
    # A featureless field has no bezel: nothing found, the seed kept, nothing measured.
    flat = surface_refine.refine(np.full((H, W), 120.0, dtype=np.float32), TRUE, band)
    assert not flat.found and flat.seed_error is None and np.array_equal(flat.quad, TRUE)


def test_a_person_across_an_edge_moves_the_line_nowhere():
    clear = surface_refine.refine(luma(_room(TRUE, seed=2)), TRUE, surface_refine.BAND * W)
    # A person standing across the left edge, a third of its length: a minority agreeing on no line.
    crossed = surface_refine.refine(luma(_room(TRUE, seed=2, person=(290, 230, 50))), TRUE, surface_refine.BAND * W)
    assert crossed.found
    assert np.linalg.norm(crossed.quad - clear.quad, axis=1).max() < 1.5


# ── The four fixtures ─────────────────────────────────────────────────────


def _clean(report: dict, found: list) -> None:
    assert found == [], [f.message for f, _p in found]
    assert report["seed_error"] is not None and report["seed_error"] < surface_refine.ALIGNMENT_MAX_SHARE
    assert report["alignment_max"] < surface_refine.ALIGNMENT_MAX_SHARE
    assert report["flicker_frames"] == 0
    assert report["sharpness"] is not None and report["sharpness"] >= screen_insert.MIN_SHARPNESS
    assert report["frames"] == FRAMES and report["refined"] is not None and len(report["refined"]) == 4


def test_a_static_bezel_is_drawn_aligned_sharp_clean_and_reported():
    frames = [_room(TRUE, seed=10 + n) for n in range(FRAMES)]
    audits, found, report, layers = _composed(frames, _insert(TRUE))
    _clean(report, found)
    assert report["residual_key"] == 0.0 and report["halo"] is None
    # Drawn into the refined quad: on the bezel the footage shows, within a pixel of the surface's own corners.
    refined = np.array(report["refined"]) * [W, H]
    assert np.linalg.norm(refined - TRUE, axis=1).max() < 1.5
    # Every frame says what it measured, as the segment keeps it.
    first = audits["frames"]["0"]
    assert set(first) >= {"alignment", "refined", "coverage", "luma", "source_luma", "residual_key", "sharpness", "halo"}
    assert first["coverage"] > 0.98 and first["covered"] == 0.0
    # The layer covers the surface and its margin, the insert's pixels where the surface is.
    x0, y0, _x1, _y1 = layers[0].warped.box
    inside = np.asarray(layers[0].warped.image)[int(TRUE[:, 1].mean()) - y0, int(TRUE[:, 0].mean()) - x0]
    assert inside[3] == 255 and inside[1] < 120, "the insert, not the green, at the surface's centre"


def test_a_bezel_the_camera_pushes_in_on_keeps_the_insert_on_it_though_the_tracker_left_it_pixels_off():
    centre = TRUE.mean(axis=0)
    truths = [centre + (TRUE - centre) * (1 + 0.004 * n) for n in range(FRAMES)]
    frames = [_room(truth, seed=20 + n) for n, truth in enumerate(truths)]
    # The planar tracker's quads: the surface followed, a few pixels off it on every frame.
    slip = np.array([3.0, -2.0])
    tracked = InsertTrack(0, tuple(tuple((float(x) / W, float(y) / H) for x, y in q + slip) for q in truths), (0.2, 0.1, 0.8, 0.9))
    _audits, found, report, _layers = _composed(frames, _insert(truths[0] + slip, track=tracked))
    _clean(report, found)
    # The tracker's error is measured (about 3.6 px of 960) and the insert drawn on the bezel all the same.
    assert report["seed_error"] == pytest.approx(np.hypot(3.0, 2.0) / W, abs=1.0 / W)
    for n, truth in enumerate(truths):
        refined = np.array(_audits["frames"][str(n)]["refined"]) * [W, H]
        assert np.linalg.norm(refined - truth, axis=1).max() < 2.0, n


def test_a_green_surface_under_uneven_tinted_light_is_one_surface():
    frames = [_room(TRUE, seed=30 + n, light=((1.35, 1.0, 0.7), (0.55, 0.75, 1.1))) for n in range(FRAMES)]
    insert = screen_insert.keyed_on(_insert(TRUE), frames[0])
    # The key's colour and its spread are read off the surface: the tolerance widens to the light's.
    assert insert.key.rgb is not None and insert.key.spread is not None and insert.key.spread > 0.0
    assert insert.key.matte_tolerance() >= insert.key.tolerance
    audits, found, report, _layers = _composed(frames, insert)
    _clean(report, found)
    assert report["residual_key"] == 0.0
    assert all(audit["coverage"] > 0.98 and audit["covered"] == 0.0 for audit in audits["frames"].values())


#: A person walking in from the left, across the surface's left and bottom edges: where they stand on frame *n*.
def _walking(n: int) -> tuple[int, int, int]:
    return 240 + 20 * n, 300, 55


def test_a_person_crossing_the_surface_stays_in_front_without_a_halo_and_turns_the_coverage_once():
    frames = [_room(TRUE, seed=40 + n, person=_walking(n)) for n in range(FRAMES)]
    audits, found, report, layers = _composed(frames, _insert(TRUE))
    _clean(report, found)
    assert all(audit["covered"] == 0.0 for audit in audits["frames"].values()), "nothing drawn over the person"
    assert report["halo"] is not None and report["halo"] <= screen_insert.MAX_HALO_SHARE
    assert report["residual_key"] == 0.0
    # The person's pixels keep their colour where they stand in front of the surface, the insert around them.
    middle = layers[FRAMES // 2]
    x0, y0, _x1, _y1 = middle.warped.box
    x, y, _r = _walking(FRAMES // 2)
    pixel = np.asarray(middle.warped.image)[y + 40 - y0, x - x0]
    assert pixel[3] == 255 and abs(int(pixel[0]) - 70) < 25 and int(pixel[1]) <= int(pixel[0]) + 6, pixel
    # The coverage falls as they come in (the noise's wobble under the threshold): never flicker.
    coverage = [audits["frames"][str(n)]["coverage"] for n in range(FRAMES)]
    assert coverage[-1] < coverage[0] - 0.002
    assert insert_checks.flicker_frames({n: audits["frames"][str(n)] for n in range(FRAMES)}) == []


def test_without_a_key_the_occlusion_matte_keeps_the_person_in_front_and_the_bezel_lock_reads_past_them():
    frames = [_room(TRUE, seed=50 + n, person=_walking(n)) for n in range(FRAMES)]
    audits, found, report, _layers = _composed(frames, _insert(TRUE, keyed=False))
    _clean(report, found)
    assert report["residual_key"] is None and report["halo"] is None
    assert all(audit["covered"] == 0.0 for audit in audits["frames"].values())


# ── What fails the render ─────────────────────────────────────────────────


def test_a_deliberately_wrong_quad_fails_the_render_with_the_frame_and_the_bezel_found():
    frames = [_room(TRUE, seed=60 + n) for n in range(FRAMES)]
    off = TRUE + np.array([0.02 * W, 0.01 * H])
    _audits, found, report, layers = _composed(frames, _insert(off))
    kinds = {f.kind: f for f, _p in found}
    assert render_checks.INSERT_ALIGNMENT in kinds
    finding = kinds[render_checks.INSERT_ALIGNMENT]
    assert finding.severity == render_checks.FAIL and finding.scene_id == "demo"
    # Named on its worst frame as the gate reads each one: the bezel read against the lock on every frame.
    worst_at = finding.data["worst_at"]
    assert 0.0 <= worst_at < FRAMES / FPS
    assert finding.message.startswith(f"insert off its bezel at t={insert_checks.clock(worst_at)}: Scene demo: its screen insert 0's quad is 2.")
    assert "the insert is drawn snapped to the bezel" in finding.message and "leave its quad out (quad auto) and save" in finding.message
    # One action, its gate's value against its threshold.
    assert finding.data["action"]["kind"] == insert_checks.GIVE_QUAD and finding.fix == finding.data["action"]["text"]
    assert finding.data["gate"] == "alignment" and finding.data["value"] > finding.data["threshold"] == surface_refine.ALIGNMENT_MAX_SHARE
    assert finding.data["frames"] == FRAMES and finding.data["worst"] > surface_refine.ALIGNMENT_MAX_SHARE
    assert report["seed_error"] == pytest.approx(np.hypot(0.02 * W, 0.01 * H) / W, abs=1.5 / W)
    # The whole render fails on it, and the gate refuses the next one of the revision.
    checks = render_checks.Checks(findings=[finding])
    assert checks.failed and render_checks.failure_summary(checks).startswith("The render failed its picture check")
    assert render_checks.open_findings(checks.as_dict(), same_quality=False)
    assert render_checks.INSERT_ALIGNMENT in render_checks.PICTURE_KINDS
    # The frame a finding shows is drawn, the given quad and the bezel found outlined on it.
    assert layers[0].evidence is not None and layers[0].evidence.size[0] > 8
    # Snapped to the bezel it found, the insert covers the surface: nothing of the key is left around it.
    assert report["residual_key"] == 0.0 and finding.data["refined"] is not None


def test_a_quad_too_small_for_its_surface_leaves_the_key_around_the_insert_and_fails():
    frames = [_room(TRUE, seed=70 + n) for n in range(FRAMES)]
    centre = TRUE.mean(axis=0)
    _audits, found, report, _layers = _composed(frames, _insert(centre + (TRUE - centre) * 0.88))
    kinds = {f.kind: f for f, _p in found}
    # Read inside the quad and RESIDUAL_MARGIN around it: the surface running past it still fails.
    assert render_checks.INSERT_RESIDUAL_KEY in kinds and report["residual_key"] > 3 * screen_insert.RESIDUAL_FAIL_SHARE
    assert not report["residual_advisory"]
    assert kinds[render_checks.INSERT_RESIDUAL_KEY].message.startswith("residual key at t=")
    # The fix, never the corners to type back: the save measures the bezel of a quad left to it.
    assert "leave its quad out (quad auto, no track) and save" in kinds[render_checks.INSERT_RESIDUAL_KEY].message


def test_a_hard_edged_matte_fails_the_render(monkeypatch):
    """A binary matte with no despill - a matte read hard, as before, neither the clamp nor the edge treatment
    (``despill.edge`` off) - leaves the key on the person's edge, on more than the 5 frames a halo needs.
    """
    frames = [_room(TRUE, seed=40 + n, person=_walking(n)) for n in range(12)]
    soft = screens.key_matte
    monkeypatch.setattr(screens, "key_matte", lambda rgb, key, tolerance=None: (soft(rgb, key, tolerance) > 0.5).astype(np.float32))
    monkeypatch.setattr(screens, "despill_clamp", lambda rgb, key, where=None: rgb)
    insert = screen_insert.keyed_on(_insert(TRUE), frames[0])
    insert = dataclasses.replace(
        insert, key=dataclasses.replace(insert.key, feather=0.0, spread=None), despill=screen_insert.Despill(edge=False)
    )
    _audits, found, report, _layers = _composed(frames, insert, state=False)
    kinds = {f.kind: f for f, _p in found}
    assert render_checks.INSERT_HALO in kinds and report["halo"] > screen_insert.MAX_HALO_SHARE
    assert kinds[render_checks.INSERT_HALO].data["frames"] >= insert_checks.HALO_MIN_FRAMES
    assert kinds[render_checks.INSERT_HALO].message.startswith("insert halo at t=")
    # Its one action: no fixture shows a key's feather moving a halo, so the feather is never the advice.
    assert kinds[render_checks.INSERT_HALO].data["action"]["kind"] == insert_checks.REGENERATE_SHOT
    assert "feather" not in kinds[render_checks.INSERT_HALO].message
    # The edge it was read on is measured, and said.
    assert report["halo_edge"] > 100 and f"({report['halo_edge']} px long)" in kinds[render_checks.INSERT_HALO].message
    checks = render_checks.Checks(findings=[f for f, _p in found])
    assert checks.failed and render_checks.INSERT_HALO in render_checks.PICTURE_KINDS


def test_flicker_is_an_alternation_within_a_few_frames_never_one_turn():
    def series(values: list[float]) -> dict[int, dict]:
        return {n: {"coverage": v, "luma": 100.0, "source_luma": 100.0} for n, v in enumerate(values)}

    # A person walking in and out: the coverage falls, then rises - one turn.
    assert insert_checks.flicker_frames(series([1.0, 0.95, 0.9, 0.86, 0.9, 0.95, 1.0])) == []
    # A matte flickering at its tolerance: down, up, down, up.
    assert insert_checks.flicker_frames(series([1.0, 0.95, 1.0, 0.95, 1.0, 0.95, 1.0])) == [1, 2, 3, 4, 5]
    # Under the threshold it is noise.
    assert insert_checks.flicker_frames(series([1.0, 0.99, 1.0, 0.99, 1.0])) == []
    # The insert's luma against its source reversing, the coverage still: a despill that flickers.
    lit = {n: {"coverage": 1.0, "luma": 100.0 + (8.0 if n % 2 else 0.0), "source_luma": 100.0} for n in range(6)}
    assert insert_checks.flicker_frames(lit) == [1, 2, 3, 4]
    # Frames not consecutive (a segment seam) are not compared across the gap.
    gapped = series([1.0, 0.95, 1.0, 0.95, 1.0])
    gapped[9] = gapped.pop(4)
    assert insert_checks.flicker_frames(gapped) == [1, 2]
    [(finding, _proof)] = insert_checks.findings(
        {KEY: {"frames": {str(n): a for n, a in series([1.0, 0.95, 1.0, 0.95, 1.0, 0.95, 1.0]).items()}, "evidence": {}}},
        {KEY: insert_checks.Placed(KEY, "demo", "Scene demo: its screen insert 0", 0.0, 1.0)},
        fps=30,
    )
    assert finding.kind == render_checks.INSERT_FLICKER and finding.data["frames"] == 5
    assert finding.message.startswith("insert flickers at t=0:00.03")


def test_a_soft_insert_fails_and_the_report_carries_the_lowest_sharpness():
    frames = {str(n): {"sharpness": 0.9 if n < 3 else 0.42, "coverage": 1.0} for n in range(6)}
    placed = {KEY: insert_checks.Placed(KEY, "demo", "Scene demo: its screen insert 0", 0.0, 1.0)}
    [(finding, _proof)] = insert_checks.findings({KEY: {"frames": frames, "evidence": {}}}, placed, fps=30)
    assert finding.kind == render_checks.INSERT_SOFT and finding.data["frames"] == 3 and finding.data["worst"] == 0.42
    assert "0.42 of its source's sharpness" in finding.message
    [report] = insert_checks.reports({KEY: {"frames": frames, "evidence": {}}}, placed, fps=30)
    assert report["sharpness"] == 0.42 and report["flicker_frames"] == 0


# ── The pieces ────────────────────────────────────────────────────────────


def test_the_despill_clamps_the_key_channel_and_never_inverts():
    key = screens.Key("green", GREEN)
    fringe = np.array([[[60.0, 140.0, 50.0], [200.0, 210.0, 190.0], [30.0, 20.0, 40.0]]], dtype=np.float32)
    out = screens.despill_clamp(fringe, key)
    assert out[0, 0].tolist() == [60.0, 60.0, 50.0], "green clamped to the max of red and blue"
    assert out[0, 1].tolist() == [200.0, 200.0, 190.0]
    assert out[0, 2].tolist() == [30.0, 20.0, 40.0], "a pixel with no green excess is untouched"
    # Where says where: outside it nothing changes.
    kept = screens.despill_clamp(fringe, key, np.array([[False, True, False]]))
    assert kept[0, 0].tolist() == [60.0, 140.0, 50.0] and kept[0, 1].tolist() == [200.0, 200.0, 190.0]
    # A magenta key lifts its weak channel to the lesser of the two it is made of.
    magenta = screens.despill_clamp(np.array([[[200.0, 40.0, 180.0]]], dtype=np.float32), screens.Key("magenta", (230, 0, 210)))
    assert magenta[0, 0].tolist() == [200.0, 180.0, 180.0]
    # The share an unmix takes out is the linear colour difference: half for a half blend, none for another colour.
    blend = 0.5 * np.array(GREEN, dtype=np.float32) + 0.5 * np.array([70.0, 50.0, 40.0], dtype=np.float32)
    share = screens.key_share(np.array([[blend, [70.0, 50.0, 40.0], GREEN]], dtype=np.float32), key)
    assert share[0, 0] == pytest.approx(0.5, abs=0.06) and share[0, 1] == pytest.approx(0.0, abs=0.06) and share[0, 2] == 1.0


def test_a_named_key_reads_its_surface_s_spread_and_a_hex_key_keeps_its_colour():
    frame = _room(TRUE, seed=3, light=((1.35, 1.0, 0.7), (0.55, 0.75, 1.1)))
    quad_px = [tuple(p) for p in TRUE]
    spread = screens.key_spread(frame, quad_px, screens.Key("green", GREEN))
    assert spread is not None and spread > 0.03
    # Lit evenly, the same surface spreads less.
    even = screens.key_spread(_room(TRUE, seed=3), quad_px, screens.Key("green", GREEN))
    assert even is not None and even < spread
    assert screens.spread_tolerance(0.3, spread) == pytest.approx(max(0.3, min(screens.MAX_SPREAD_TOLERANCE, screens.SPREAD_FACTOR * spread)))
    assert screens.spread_tolerance(0.3, None) == 0.3
    hexed = screen_insert.keyed_on(dataclasses.replace(_insert(TRUE), key=screen_insert.KeySpec("#20ff1e")), frame)
    assert hexed.key.key().rgb == (0x20, 0xFF, 0x1E) and hexed.key.spread is not None
    # A surface of another colour has no spread of this key: the author's tolerance stands.
    assert screens.key_spread(_room(TRUE, seed=4), quad_px, screens.Key("blue", (0, 71, 187))) is None


def test_the_warp_is_supersampled_and_the_sharpness_read_against_the_source_at_the_drawn_size(monkeypatch):
    frames = [_room(TRUE, seed=80)]
    _audits, _found, sharp, _layers = _composed(frames, _insert(TRUE))
    monkeypatch.setattr(screen_insert, "SUPERSAMPLE_BUDGET", 0)
    _audits, _found, plain, _layers = _composed(frames, _insert(TRUE))
    assert sharp["sharpness"] >= screen_insert.MIN_SHARPNESS
    assert sharp["sharpness"] > plain["sharpness"], "read at twice the size and brought down once, the strokes keep their edges"
    # The warp's edge is antialiased either way, and its interior exact.
    warped = screen_insert.warp(Image.new("RGBA", (400, 300), (30, 120, 220, 255)), [(x / W, y / H) for x, y in TRUE], (W, H), "cover")
    pixels = np.asarray(warped.image)
    x0, y0, _x1, _y1 = warped.box
    centre = TRUE.mean(axis=0)
    assert pixels[int(centre[1]) - y0, int(centre[0]) - x0].tolist() == [30, 120, 220, 255]
    assert 0 < pixels[..., 3].astype(int).max() == 255 and ((pixels[..., 3] > 0) & (pixels[..., 3] < 255)).any()


def test_a_matted_insert_covers_the_quad_grown_and_the_page_places_the_same_box():
    quad = [(x / W, y / H) for x, y in TRUE]
    box = screen_insert.grown_box(quad, W / H)
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    assert box[0] == (min(xs) - screen_insert.BOX_MARGIN, min(ys) - screen_insert.BOX_MARGIN * W / H)
    assert box[2] == (max(xs) + screen_insert.BOX_MARGIN, max(ys) + screen_insert.BOX_MARGIN * W / H)
    # Composed with footage and no box given, the layer covers that very box; given one, it covers the given one.
    frame = _room(TRUE, seed=5)
    layer = screen_insert.compose(_insert(TRUE), _ui(), (W, H), 0, footage=frame)
    assert layer.warped.box == screen_insert.bbox(box, W, H)
    told = dataclasses.replace(_insert(TRUE), box=tuple(box))
    assert told.quads(0)[1] == [tuple(p) for p in box]
    # The page reads the box the render puts on the payload, names each insert's node and tells where it is drawn.
    from app.services.video_studio.compositor_page import PAGE_JS

    assert "insertBox(ins.box || ins.quad, W, H)" in PAGE_JS and "node.dataset.insert = ins.src;" in PAGE_JS
    assert "insertRects() {" in PAGE_JS
    assert "rects[node.dataset.insert] = [r.left - stage.left, r.top - stage.top, r.width, r.height, Math.round(shown * 1000) / 1000," in PAGE_JS
    # And what its own scene draws over it, as the page's own check reads what is on screen.
    assert "ink = ink || onScreen(built.layer, S.scenes[i], S.T)" in PAGE_JS and "...(over.length ? [over] : [])];" in PAGE_JS


def test_the_render_tells_every_matted_insert_its_box_and_a_range_worker_reads_it_back():
    from app.services.video_studio import render_ranges

    told = dataclasses.replace(_insert(TRUE), box=tuple(screen_insert.grown_box([(x / W, y / H) for x, y in TRUE], W / H)))
    assets = compositor.CompositorAssets(inserts={KEY: told})
    published = json.loads(json.dumps(render_ranges._manifest(assets, lambda path: path), default=str))
    assert published["inserts"][KEY]["box"] == [list(p) for p in told.box]
    assert published["inserts"][KEY]["key"]["spread"] is None and published["inserts"][KEY]["key"]["color"] == "green"


def test_the_crops_are_picked_at_the_insert_s_first_middle_and_last_frame_and_named_in_the_answer(tmp_path):
    placed = {KEY: insert_checks.Placed(KEY, "demo", "Scene demo: its screen insert 0", 2.0, 6.0)}
    frames = insert_checks.crop_frames(placed, fps=30)
    # In once faded in (0.2 s), out before the fade out, the middle between.
    assert frames == {KEY: {67, 120, 173}}
    # A preview's window cuts them to the frames it draws; a short insert takes one frame.
    assert insert_checks.crop_frames(placed, fps=30, frame_range=(100, 150)) == {KEY: {100, 124, 149}}
    assert insert_checks.crop_frames({KEY: insert_checks.Placed(KEY, "demo", "x", 2.0, 2.3)}, fps=30) == {KEY: {64}}
    # The frame server keeps a crop of the captured frame at the render's resolution, and hands it on with the audits.
    shot = Image.new("RGB", (W, H), (10, 20, 30))
    ImageDraw.Draw(shot).rectangle((300, 120, 660, 410), fill=(200, 40, 40))
    import io

    buffer = io.BytesIO()
    shot.save(buffer, format="PNG")
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=tmp_path / "evidence", crops={KEY: [67]})
    import asyncio

    asyncio.run(server.crop(67, buffer.getvalue(), {KEY: [150.0, 60.0, 180.0, 145.0]}, (W / 2, H / 2)))
    taken = server.take_audits(60, 70)
    [name] = taken[KEY]["crops"].values()
    crop = Image.open(tmp_path / "evidence" / name)
    assert crop.size == (360, 290) and crop.getpixel((100, 100)) == (200, 40, 40)
    # Read back merged from the segments, the crops become the report's, named first, middle, last.
    directory = tmp_path / compositor.SEGMENTS_DIR
    (directory / compositor.EVIDENCE_DIR).mkdir(parents=True)
    for n in (67, 120, 173):
        (directory / compositor.EVIDENCE_DIR / f"c{n}.png").write_bytes(b"png")
    part = {KEY: {"frames": {str(n): {"coverage": 1.0, "alignment": 0.001, "refined": [[0.3, 0.2], [0.7, 0.2], [0.7, 0.7], [0.3, 0.7]]} for n in (67, 120, 173)}, "evidence": {}, "crops": {str(n): f"c{n}.png" for n in (67, 120, 173)}}}
    (directory / (Path(compositor.segment_name(60, 180)).stem + compositor.AUDIT_SUFFIX)).write_text(json.dumps(part), encoding="utf-8")
    merged = compositor.segment_audits(directory, [(60, 180)])
    assert merged[KEY]["crops"] == {n: str(directory / compositor.EVIDENCE_DIR / f"c{n}.png") for n in (67, 120, 173)}
    [report] = insert_checks.reports(merged, placed, fps=30)
    assert [c["word"] for c in report["crops"]] == ["first", "middle", "last"] and report["crops"][1]["at"] == 4.0
    assert report["seed_error"] == 0.001 and report["frames"] == 3
    # Stored, each crop's file id travels on the report; the answer names them and the retention deletes them.
    for crop, file_id in zip(report["crops"], ("f1", "f2", "f3"), strict=True):
        crop.pop("path")
        crop["file_id"] = file_id
    checks = render_checks.Checks(inserts=[report]).as_dict()
    assert render_checks.insert_crop_ids(checks) == ["f1", "f2", "f3"] and render_checks.frame_file_ids(checks) == ["f1", "f2", "f3"]
    [line] = render_checks.insert_lines(checks)
    assert line.startswith("Insert Scene demo: its screen insert 0 (refined quad [[0.3, 0.2]")
    assert "the quad 0.10% of the frame's width off it" in line and "first frame file `f1`" in line and "last frame file `f3`" in line
    assert "Insert Scene demo" in render_checks.format_checks(checks)
    row = SimpleNamespace(file_id=None, poster_file_id=None, captions_file_id=None, frame_file_ids=None, checks=checks)
    from app.services.video_studio import render_retention

    assert render_retention.render_files(row) == []  # ids that are not UUIDs are skipped, never raised on
