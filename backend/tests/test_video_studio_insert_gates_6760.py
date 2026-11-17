"""A screen insert's foreground read at the frame's own resolution, and the three gates.

On synthetic footage, no provider, no browser:

* a dark UI drawn on the footage's screen, a bright subject standing still in
  front of it - across the screen's bottom edge, a fifth of the quad -, its
  edge soft and tufted: strands a pixel and a half wide its grid opens away,
  for 60 frames; an unkeyed insert of a dark UI ruled every 40 px with 1 px
  white lines. The mask read at the frame's own resolution and closed there
  (``Surface.front``) leaves no rule pixel inside the subject and files no
  finding; the grid's mask alone - the earlier drawing - draws the rules
  across its tufts as dashes, which ``insert_through_foreground`` reads, a
  block like an audio finding, with its frame and a 100 % crop;
* a mask whose confidence is 0.3 on a fifth of the frames, what stands in
  front covering more than 2 % of the quad there, is ``insert_ghost`` with
  one action; at 0.8 it is a fact;
* a flat green panel keyed, a 10 % reflection of it on a glossy floor below:
  ``key_spill_outside_quad`` with the whole frame; the reflection painted
  out, nothing - nor another screen of the scene, nor the subject's own
  colour.
"""

from __future__ import annotations

import asyncio
import functools
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from app.services.video_studio import compositor, insert_checks, quality, render_checks, screen_insert

W, H = 960, 540
FPS = 24.0
FRAMES = 60
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
#: The screen's corners (pixels): an 840 x 460 quad, read on its grid at about 2.3 px a grid pixel - as coarse as a
#: 1080p frame's.
QUAD = np.array([(60, 40), (900, 40), (900, 500), (60, 500)], dtype=float)
RULE_EVERY = 40
#: The subject: an ellipse across the screen's bottom edge, its own edge soft over a pixel, then strands 1.5 px wide
#: every 5 px round it, 6 px long, fading to 30 % at their tips.
CENTRE, AXES = (480.0, 470.0), (222.0, 172.0)
STRAND_LENGTH, STRAND_PERIOD, STRAND_WIDTH, BODY_SOFT, STRAND_TIP = 6.0, 5.0, 1.5, 1.0, 0.3


def _subject_alpha() -> np.ndarray:
    """The subject's coverage of each pixel (0-1): fur, which its grid reads to its body and opens its tufts away."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    dx, dy = xs - CENTRE[0], ys - CENTRE[1]
    angle = np.arctan2(dy, dx)
    radius = 1.0 / np.sqrt((np.cos(angle) / AXES[0]) ** 2 + (np.sin(angle) / AXES[1]) ** 2)
    past = np.hypot(dx, dy) - radius
    rays = round(2.0 * math.pi * max(AXES) / STRAND_PERIOD)
    strands = (((angle * rays / (2.0 * math.pi)) % 1.0) < STRAND_WIDTH / STRAND_PERIOD).astype(np.float32)
    body = ndimage.gaussian_filter((past <= 0.0).astype(np.float32), BODY_SOFT)
    fade = np.clip(1.0 - (1.0 - STRAND_TIP) * past / STRAND_LENGTH, 0.0, 1.0)
    return np.clip(np.maximum(body, strands * fade * ((past > 0.0) & (past <= STRAND_LENGTH))), 0.0, 1.0)


SUBJECT = _subject_alpha()


def _screen() -> np.ndarray:
    """The footage's screen as the model drew it: a dark UI - panels a few levels apart - in its bezel, a dim wall round
    it: the screen's own edge is the bezel the lock finds."""
    frame = np.empty((H, W, 3), dtype=np.float32)
    frame[...] = (62.0, 63.0, 66.0)
    (x0, y0), (x1, y1) = QUAD[0].astype(int), QUAD[2].astype(int)
    frame[y0 - 14 : y1 + 14, x0 - 14 : x1 + 14] = (52.0, 54.0, 58.0)
    frame[y0:y1, x0:x1] = (22.0, 26.0, 34.0)
    for top in range(y0 + 20, y1 - 40, 70):
        frame[top : top + 46, x0 + 24 : x1 - 24] = (30.0, 34.0, 44.0)
    return frame


SCREEN = _screen()


@functools.lru_cache(maxsize=12)
def _frame(n: int) -> np.ndarray:
    """Footage frame *n*: the subject still over the screen, the encoder's noise its own on each frame."""
    rng = np.random.default_rng(6760 + n)
    alpha = SUBJECT[..., None]
    fur = np.array([236.0, 232.0, 224.0], dtype=np.float32) + rng.normal(0.0, 4.0, (H, W, 1)).astype(np.float32)
    frame = SCREEN * (1.0 - alpha) + fur * alpha + rng.normal(0.0, 2.0, (H, W, 3)).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _ruled() -> Image.Image:
    """The insert's source: a dark UI of the quad's size, ruled every 40 px with a 1 px white line."""
    picture = np.empty((460, 840, 3), dtype=np.uint8)
    picture[...] = (20, 24, 32)
    picture[RULE_EVERY // 2 :: RULE_EVERY] = 255
    return Image.fromarray(picture, "RGB")


def _quad() -> tuple[tuple[float, float], ...]:
    return tuple((float(x) / W, float(y) / H) for x, y in QUAD)


def _insert(**values) -> screen_insert.InsertSource:
    quad = _quad()
    return screen_insert.InsertSource(
        "p.png",
        "image",
        quad,
        footage=screen_insert.Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        plate=screen_insert.plate_frames(0, FRAMES - 1),
        label=LABEL,
        **values,
    )


def _drawn(frames: range, *, evidence: Path | None = None, read: Callable[[int, screen_insert.Layer], Any] | None = None) -> dict:
    """*frames* composed as the frame server composes them - the clean surface read over the plate, the bezel locked on
    the first frame, the matte's window read ahead -, their audits kept by the frame server's own evidence rule, and
    what *read* takes of each (``{frame: read(frame, layer)}``)."""
    insert = _insert()
    surface = screen_insert.Surface.of_plate([(_frame(n), QUAD) for n in insert.plate])
    lock = screen_insert.static_lock(insert, _frame(0), (W, H))
    state = screen_insert.Matting()
    picture = _ruled()
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=evidence)
    taken = {}
    for n in frames:
        ahead = [(n + k, _frame(n + k)) for k in (1, 2) if n + k < FRAMES]
        layer = screen_insert.compose(
            insert, picture, (W, H), n, footage=_frame(n), surface=surface, lock=lock, state=state, footage_ahead=ahead, evidence=evidence is not None
        )
        assert layer is not None
        if read is not None:
            taken[n] = read(n, layer)
        asyncio.run(server._audited(KEY, n, layer.audit, layer.pictures()))
    return {"surface": surface, "server": server, "read": taken}


def _gate(server: compositor.FrameServer, frames: int, folder: Path | None = None, *, keyed: bool = False) -> tuple[list, dict, dict]:
    """The render's findings, the insert's report and its frames' audits off *server*'s audits (taken: handed on and
    forgotten by the server), as the render reads them."""
    taken = server.take_audits(0, frames)[KEY]
    where = folder if folder is not None else Path(".")
    audits = {
        KEY: {
            "frames": taken["frames"],
            "evidence": {kind: [{**proof, "path": str(where / proof["file"])}] for kind, proof in taken["evidence"].items()},
        }
    }
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, frames / FPS, keyed=keyed)}
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return insert_checks.findings(audits, placed, fps=FPS), report, taken["frames"]


def _composed(layer: screen_insert.Layer, n: int) -> tuple[np.ndarray, np.ndarray]:
    """``(the box as composited, the footage under it)``."""
    x0, y0, x1, y1 = layer.warped.box
    drawn = np.asarray(layer.warped.image, dtype=np.float32)
    alpha = drawn[..., 3:4] / 255.0
    region = _frame(n)[y0:y1, x0:x1].astype(np.float32)
    return drawn[..., :3] * alpha + region * (1.0 - alpha), region


def _rule_pixels(n: int, layer: screen_insert.Layer) -> int:
    """The pixels inside the subject - where it covers a tenth of a pixel at least, 20 grey levels over the screen - and
    inside the quad where a rule of the source shows: the source the layer carries bright there (a rule, wherever the
    bezel lock put it) and the frame as composited brighter than the footage by more than 8 grey levels."""
    x0, y0, x1, y1 = layer.warped.box
    composed, region = _composed(layer, n)
    inside = screen_insert._coverage(QUAD, layer.warped.box) > 0.5
    rules = np.asarray(layer.warped.image.convert("RGB").convert("L"), dtype=np.float32) > 128.0
    subject = (SUBJECT[y0:y1, x0:x1] >= 0.1) & inside
    return int((subject & rules & ((composed - region).mean(axis=-1) > 8.0)).sum())


# ── The mask at the frame's own resolution, and what the audience sees through it ──


def test_the_fixture_is_a_fifth_of_the_quad_on_a_grid_as_coarse_as_a_1080p_frame_s():
    inside = screen_insert._coverage(QUAD, (0, 0, W, H)) > 0.5
    assert 0.18 <= float((SUBJECT[inside] >= 0.5).mean()) <= 0.22
    surface = screen_insert.Surface.of(_frame(0), QUAD)
    assert surface.grid[0] == screen_insert.SURFACE_SIDE
    # Closed with a kernel 3 px wide at 1080p, scaled with the frame, never under 3.
    assert screen_insert.close_width(1.0) == 3 and screen_insert.close_width(H / 1080.0) == 3 and screen_insert.close_width(2.0) == 7
    # Earlier renders are drawn again, and earlier quality reports read again.
    assert screen_insert.DRAWING_VERSION >= 14 and quality.QUALITY_REPORT_VERSION >= 23


def test_a_still_subject_is_read_at_the_frame_s_resolution_and_no_rule_of_the_source_crosses_it(tmp_path):
    drawn = _drawn(range(FRAMES), evidence=tmp_path, read=_rule_pixels)
    assert drawn["read"] == dict.fromkeys(range(FRAMES), 0)
    found, report, frames = _gate(drawn["server"], FRAMES, tmp_path)
    assert found == [], [finding.message for finding, _proof in found]
    assert all((audit.get("through") or 0.0) == 0.0 and audit.get("covered") == 0.0 for audit in frames.values())
    # A bright subject is told from a dark screen by its own difference: a fact, never a ghost.
    assert report["foreground"]["confidence"]["lowest"] >= screen_insert.LOW_FRONT_CONFIDENCE
    assert report["foreground"]["confidence"]["ghost"] == 0


def test_the_grid_s_mask_alone_draws_the_rules_across_the_tufts_and_the_gate_reads_it_with_its_frame_and_crop(monkeypatch, tmp_path):
    # The earlier drawing: the grid's mask brought to the frame, never read again there.
    monkeypatch.setattr(screen_insert.Surface, "_full", lambda self, footage, quad_px, box, seed, read: seed)
    before = _drawn(range(4), read=lambda n, layer: layer)
    assert sum(_rule_pixels(n, layer) for n, layer in before["read"].items()) > 0
    monkeypatch.undo()
    surface = before["surface"]
    measured = {}
    for n, layer in before["read"].items():
        box = layer.warped.box
        inside = screen_insert._coverage(QUAD, box) > 0.5
        mask = surface.front(_frame(n), QUAD, box, screen_insert.OCCLUSION_FEATHER * H / 1080.0).foreground
        composed, region = _composed(layer, n)
        shown = np.asarray(layer.warped.image.convert("RGB"), dtype=np.float32)
        share, hits = screen_insert.through_share(composed, region, shown, screen_insert.front_core(mask, inside, 1), H / 1080.0)
        measured[n] = (share, hits, composed)
    worst = max(measured, key=lambda n: measured[n][0])
    share, hits, composed = measured[worst]
    assert share > screen_insert.MAX_THROUGH_SHARE == 0.001
    crop = screen_insert._crop_around(composed, hits, H / 1080.0, 4)
    crop.save(tmp_path / "through-crop.png")
    Image.fromarray(np.clip(composed, 0, 255).astype(np.uint8)).save(tmp_path / "through.jpg")
    audits = {
        KEY: {
            "frames": {n: {"through": measured[n][0]} for n in measured},
            "evidence": {
                "through": [{"frame": worst, "badness": share, "path": str(tmp_path / "through.jpg")}],
                "through_crop": [{"frame": worst, "badness": share, "path": str(tmp_path / "through-crop.png")}],
            },
        }
    }
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 4 / FPS)}
    [(finding, proof)] = [(f, p) for f, p in insert_checks.findings(audits, placed, fps=FPS) if f.kind == render_checks.INSERT_THROUGH_FOREGROUND]
    assert finding.message.startswith(f"insert seen through the foreground at t=0:00.{round(100 * finding.data['worst_at']):02d}: {LABEL}")
    assert "its worst stretch kept at 100%" in finding.message and proof == str(tmp_path / "through.jpg")
    assert finding.data["worst_frame"] == worst and finding.data["action"]["kind"] == insert_checks.REGENERATE_SHOT
    # A block like an audio finding: a picture finding that fails the render, never the harness's.
    assert finding.severity == render_checks.FAIL and finding.kind in render_checks.PICTURE_KINDS
    assert render_checks.Checks(findings=[finding]).failed and not render_checks.is_unexplained(finding)
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    assert report["through"]["frame"] == worst and report["through_crop"]["path"] == str(tmp_path / "through-crop.png")
    assert "through-crop" not in render_checks.insert_crop_ids(render_checks.Checks(inserts=[report]).as_dict())
    report["through_crop"]["file_id"] = "file-through"
    assert "file-through" in render_checks.insert_crop_ids(render_checks.Checks(inserts=[report]).as_dict())
    [line] = [m.line() for m in insert_checks.classify(report, {render_checks.INSERT_THROUGH_FOREGROUND}) if "strokes drawn across" in m.text]
    assert line.startswith("finding (insert_through_foreground): its source's own strokes drawn across what stands in front")


def test_the_insert_forced_over_the_subject_is_read_by_the_frame_server_with_its_crop(monkeypatch, tmp_path):
    # The matte rule broken: the insert drawn whole whatever the mask says - its strokes across the subject's core.
    monkeypatch.setattr(screen_insert, "in_front", lambda share, core: np.ones_like(share))
    drawn = _drawn(range(3), evidence=tmp_path)
    server = drawn["server"]
    assert (KEY, "through") in server.evidence and (KEY, "through_crop") in server.evidence
    assert Path(server.evidence[(KEY, "through_crop")][2]).is_file()
    found, report, _frames = _gate(server, 3, tmp_path)
    kinds = {finding.kind for finding, _proof in found}
    assert {render_checks.INSERT_THROUGH_FOREGROUND, render_checks.INSERT_FOREGROUND} <= kinds
    with Image.open(report["through_crop"]["path"]) as shown:
        assert shown.format == "PNG" and min(shown.size) >= screen_insert.HALO_CROP_MIN_PX


# ── A mask too unsure of what stands in front: a ghost ──


def _confident(share_low: float, low: float, *, frames: int = FRAMES, covers: float = 0.2) -> dict:
    """Audits of *frames* frames: the mask *low* sure on the first *share_low* of them, 0.95 on the rest, what stands in
    front covering *covers* of the quad on every one."""
    lows = round(share_low * frames)
    return {
        KEY: {
            "frames": {n: {"covered": 0.0, "front_confidence": low if n < lows else 0.95, "foreground_share": covers} for n in range(frames)},
            "evidence": {},
        }
    }


@pytest.mark.parametrize("keyed", [False, True])
def test_a_mask_0_3_sure_on_a_fifth_of_the_frames_is_insert_ghost_with_one_action(keyed):
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, FRAMES / FPS, keyed=keyed)}
    audits = _confident(0.2, 0.3)
    [(finding, _proof)] = insert_checks.findings(audits, placed, fps=FPS)
    assert finding.kind == render_checks.INSERT_GHOST and finding.severity == render_checks.FAIL
    assert finding.kind in render_checks.PICTURE_KINDS and not render_checks.is_unexplained(finding)
    assert finding.data["frames"] == 12 and finding.data["of"] == FRAMES and finding.data["lowest"] == 0.3
    chosen = finding.data["action"]
    assert chosen["kind"] == insert_checks.REGENERATE_SHOT and finding.fix == chosen["text"]
    # One action: a keyed surface for an unkeyed insert, a subject contrasting with the key for a keyed one.
    assert ("key: green" in chosen["text"]) is not keyed and ("far from the key's" in chosen["text"]) is keyed
    assert finding.message.startswith(f"insert ghost at t=0:00.00: {LABEL}'s mask of what stands in front of its surface is 30% sure")
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    [line] = [m.line() for m in insert_checks.classify(report, {render_checks.INSERT_GHOST}) if "what stands in front covers" in m.text]
    assert line.startswith("finding (insert_ghost): ") and "sure at its lowest" in line


def test_a_mask_0_8_sure_a_ghost_on_too_few_frames_or_on_too_little_of_the_quad_stays_a_fact():
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, FRAMES / FPS)}
    for audits in (_confident(0.2, 0.8), _confident(0.05, 0.3), _confident(0.2, 0.3, covers=0.02)):
        assert insert_checks.findings(audits, placed, fps=FPS) == []
    [report] = insert_checks.reports(_confident(0.2, 0.8), placed, fps=FPS)
    [line] = [m.line() for m in insert_checks.classify(report) if "what stands in front covers" in m.text]
    assert line.startswith("fact: ") and "80% sure at its lowest" in line


def test_a_ghost_frame_is_kept_as_evidence_by_the_frame_server(monkeypatch, tmp_path):
    reading = screen_insert.Surface._occluding

    def unsure(self, footage, quad_px):
        read = reading(self, footage, quad_px)
        return screen_insert.Occlusion(read.mask, 0.3 if read.confidence is not None else None, read.gain, read.spread)

    monkeypatch.setattr(screen_insert.Surface, "_occluding", unsure)
    drawn = _drawn(range(2), evidence=tmp_path)
    assert Path(drawn["server"].evidence[(KEY, "ghost")][2]).is_file()
    found, _report, frames = _gate(drawn["server"], 2, tmp_path)
    assert all(audit["front_confidence"] == 0.3 for audit in frames.values())
    [(_ghost, proof)] = [(f, p) for f, p in found if f.kind == render_checks.INSERT_GHOST]
    assert proof and Path(proof).is_file()


# ── The key past the quad ─────────────────────────────────────────────────

#: A flat green panel on a wall, a glossy floor below it.
PANEL = np.array([(300, 80), (660, 80), (660, 300), (300, 300)], dtype=float)
GREEN = np.array([30.0, 210.0, 60.0], dtype=np.float32)
FLOOR = np.array([40.0, 40.0, 44.0], dtype=np.float32)


def _panel_frame(n: int, *, reflection: bool = True) -> np.ndarray:
    """The panel and its floor; with *reflection*, the floor reflects the panel at a tenth of its light below it."""
    rng = np.random.default_rng(76 + n)
    frame = np.empty((H, W, 3), dtype=np.float32)
    frame[...] = (150.0, 146.0, 140.0)
    frame[340:, :] = FLOOR
    frame[70:310, 290:670] = (50.0, 50.0, 54.0)
    frame[80:300, 300:660] = GREEN
    if reflection:
        frame[360:480, 300:660] = FLOOR * 0.9 + GREEN * 0.1
    frame += rng.normal(0.0, 1.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _keyed_audits(frames: list[np.ndarray], tmp_path: Path, **values) -> tuple[dict, compositor.FrameServer]:
    quad = tuple((float(x) / W, float(y) / H) for x, y in PANEL)
    insert = screen_insert.InsertSource(
        "p.png",
        "image",
        quad,
        key=screen_insert.KeySpec("green", 0.3, 1.5),
        footage=screen_insert.Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label=LABEL,
        **values,
    )
    insert = screen_insert.keyed_on(insert, frames[0])
    fill = screen_insert.static_fill(insert, frames[0], (W, H))
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=tmp_path)
    picture = Image.new("RGB", (720, 440), (24, 28, 36))
    audits = {}
    for n, frame in enumerate(frames):
        layer = screen_insert.compose(insert, picture, (W, H), n, footage=frame, fill=fill, evidence=True)
        assert layer is not None
        audits[n] = layer.audit
        asyncio.run(server._audited(KEY, n, layer.audit, layer.pictures()))
    return audits, server


def test_a_green_panel_s_reflection_on_the_floor_is_key_spill_outside_quad_with_the_whole_frame(tmp_path):
    audits, server = _keyed_audits([_panel_frame(n) for n in range(3)], tmp_path)
    assert all(audit.spills_key for audit in audits.values())
    x0, y0, x1, y1 = audits[0].key_spill_box
    assert 0.29 <= x0 <= 0.33 and 0.66 <= y0 <= 0.68 and 0.67 <= x1 <= 0.7 and 0.87 <= y1 <= 0.9
    found, report, _frames = _gate(server, 3, tmp_path, keyed=True)
    [(finding, proof)] = [(f, p) for f, p in found if f.kind == render_checks.KEY_SPILL_OUTSIDE_QUAD]
    assert finding.severity == render_checks.FAIL and finding.kind in render_checks.PICTURE_KINDS
    assert finding.data["action"]["kind"] == insert_checks.REGENERATE_SHOT and "the whole frame kept" in finding.message
    # Its frame is the whole footage frame, the key past the quad marked: what the box's proof never holds.
    with Image.open(proof) as shown:
        assert shown.size == (W, H)
    assert report["key_spill"]["frames"] == 3
    [line] = [m.line() for m in insert_checks.classify(report, {render_checks.KEY_SPILL_OUTSIDE_QUAD}) if "past the quad" in m.text]
    assert line.startswith("finding (key_spill_outside_quad): its key colour past the quad on ")


def test_the_reflection_painted_out_another_screen_or_the_subject_s_own_colour_is_no_spill(tmp_path):
    clean, _server = _keyed_audits([_panel_frame(n, reflection=False) for n in range(2)], tmp_path / "clean")
    assert not any(audit.spills_key for audit in clean.values())
    # A second screen of the scene where the reflection is: its key is its own.
    other = ((300 / W, 360 / H), (660 / W, 360 / H), (660 / W, 480 / H), (300 / W, 480 / H))
    screens, _server = _keyed_audits([_panel_frame(n) for n in range(2)], tmp_path / "screens", others=(other,))
    assert not any(audit.spills_key for audit in screens.values())
    # The subject's own colour, declared: never the key's spill.
    own, _server = _keyed_audits(
        [_panel_frame(n) for n in range(2)], tmp_path / "own", despill=screen_insert.Despill(own_hues=("green",))
    )
    assert not any(audit.spills_key for audit in own.values())
    # The source's own key hue inside the drawn quad is by design.
    lines = insert_checks.classify({"source_key_hue": 0.02, "drawn_by": {}})
    assert any(m.line().startswith("by design (its source's own colours): the source's own key hue on 2.00%") for m in lines)
