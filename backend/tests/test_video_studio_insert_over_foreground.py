"""A screen insert is never drawn over what stands in front of its surface, and the gate says so when it is.

On synthetic footage, no provider, no browser: a flat dark screen behind a
lighter bezel, a dark textured object - black fur - crossing it, its body
across the screen's bottom edge, its head over the screen; an unkeyed insert
of a dark dashboard. What is proven:

* the mask finds the whole object: its head over the dark screen differs
  from it by less than the strong threshold, and is in front by its link to
  the body that crossed the rim (``Surface.occluded``'s hysteresis), never
  cut at the screen's edge; the thin ring of weaker change round a strong
  edge is that edge's blur, never grown into;
* the surface's clean reference is read over the insert's frames
  (``Surface.of_plate``): the part of the object standing in front on the
  frame the insert comes in on is no part of the surface;
* the mask mattes: on every frame, the insert is drawn on none of the
  object's pixels, and the gate files nothing of it drawn over - its
  confidence low, dark on dark, on more than 5 % of the frames while it covers
  more than 2 % of the quad: ``insert_ghost``, the one finding;
* the same object with the insert forced over it (the matte rule broken)
  fails ``insert_foreground`` - "insert over foreground" - with the frame
  and its crop at 100 %;
* a keyed subject the key reads part key is never seen through the insert,
  its edge keeping the key's share of each pixel;
* end to end, through the frame server: the plate read off the footage, the
  object in front on every frame served.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from app.services.video_studio import compositor, insert_checks, render_checks, render_ranges, screen_insert, screens

W, H = 640, 360
FPS = 24.0
FRAMES = 12
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
#: The screen's corners (pixels), its bezel a lighter grey round it, the room past it.
QUAD = np.array([(200, 80), (440, 80), (440, 253), (200, 253)], dtype=float)
SCREEN = (26, 28, 34)
BEZEL = (70, 72, 78)
FUR = 12.0


def _object(n: int, start: int = 167) -> np.ndarray:
    """Where the object stands on frame *n* (boolean): a body across the screen's bottom edge, a head over the screen,
    walking left to right from *start* - already in front of the screen's bottom left on the frame the insert comes in
    on."""
    x = start + int(306 * n / (FRAMES - 1))
    mask = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((x - 47, 233, x + 47, 313), fill=255)
    draw.ellipse((x - 25, 187, x + 25, 240), fill=255)
    return np.asarray(mask) > 127


def _frame(n: int, rng: np.random.Generator, start: int = 167) -> np.ndarray:
    picture = Image.fromarray((rng.random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = QUAD.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (QUAD - centre) * 1.05], fill=BEZEL)
    draw.polygon([tuple(p) for p in QUAD], fill=SCREEN)
    frame = np.asarray(picture).astype(np.float32)
    fur = FUR + rng.normal(0.0, 5.0, (H, W, 1)).astype(np.float32)
    shape = _object(n, start)
    frame[shape] = np.repeat(fur, 3, axis=2)[shape]
    frame = np.asarray(Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.0))).astype(np.float32)
    frame += rng.normal(0.0, 3.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _frames(start: int = 167) -> list[np.ndarray]:
    rng = np.random.default_rng(6581)
    return [_frame(n, rng, start) for n in range(FRAMES)]


def _ui() -> Image.Image:
    """A dark dashboard of the quad's proportions: lines of text on a dark ground."""
    picture = Image.new("RGB", (960, 692), (22, 26, 34))
    draw = ImageDraw.Draw(picture)
    rng = np.random.default_rng(5)
    for row in range(30, 660, 22):
        x = 40
        while x < 900:
            w = int(rng.integers(16, 90))
            draw.rectangle((x, row, x + w, row + 8), fill=(200, 205, 215))
            x += w + 12
    return picture


def _quad() -> tuple[tuple[float, float], ...]:
    return tuple((float(x) / W, float(y) / H) for x, y in QUAD)


def _insert(footage: str = "f", **values) -> screen_insert.InsertSource:
    quad = _quad()
    return screen_insert.InsertSource(
        "p.png",
        "image",
        quad,
        footage=screen_insert.Footage(footage, "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        plate=screen_insert.plate_frames(0, FRAMES - 1),
        label=LABEL,
        **values,
    )


def _composed(frames: list[np.ndarray], evidence: Path | None = None) -> tuple[list, dict, list, dict]:
    """Every frame composed as the frame server composes it - the clean surface read over the plate, the bezel locked
    on the first frame, the matte's window read ahead - its audits kept by the frame server's own evidence rule."""
    insert = _insert()
    corners = [(x * W, y * H) for x, y in insert.quad]
    surface = screen_insert.Surface.of_plate([(frames[n], corners) for n in insert.plate])
    lock = screen_insert.static_lock(insert, frames[0], (W, H))
    state = screen_insert.Matting()
    picture = _ui()
    server = compositor.FrameServer(compositor.CompositorAssets(), "<html></html>", evidence_dir=evidence)
    layers = []
    for n, frame in enumerate(frames):
        ahead = [(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)]
        layer = screen_insert.compose(
            insert, picture, (W, H), n, footage=frame, surface=surface, lock=lock, state=state, footage_ahead=ahead, evidence=evidence is not None
        )
        assert layer is not None
        layers.append(layer)
        asyncio.run(server._audited(KEY, n, layer.audit, layer.pictures()))
    taken = server.take_audits(0, len(frames))[KEY]
    folder = evidence if evidence is not None else Path(".")
    audits = {
        KEY: {
            "frames": taken["frames"],
            "evidence": {kind: [{**proof, "path": str(folder / proof["file"])}] for kind, proof in taken["evidence"].items()},
        }
    }
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, len(frames) / FPS)}
    found = insert_checks.findings(audits, placed, fps=FPS)
    [report] = insert_checks.reports(audits, placed, fps=FPS)
    return layers, audits, found, report


def _drawn_on_object(layer: screen_insert.Layer, n: int) -> float:
    """The most of the insert drawn on any pixel of the object inside the quad, its antialiased edge left out."""
    x0, y0, x1, y1 = layer.warped.box
    alpha = np.asarray(layer.warped.image, dtype=np.float32)[..., 3] / 255.0
    inside = screen_insert._coverage(QUAD, layer.warped.box) > 0.5
    core = ndimage.binary_erosion(_object(n)[y0:y1, x0:x1] & inside, iterations=2)
    return float(alpha[core].max()) if core.any() else 0.0


# ── The mask ──────────────────────────────────────────────────────────────


def _on_grid(mask: np.ndarray, grid: tuple[int, int]) -> np.ndarray:
    """*mask* (the frame's pixels) read on a surface's grid, as ``Surface.occluded`` reads the frame."""
    return screen_insert.rectified(mask.astype(np.uint8)[..., None].repeat(3, axis=2) * 255, QUAD, grid)[..., 0] > 127


def test_a_dark_object_s_head_over_a_dark_screen_is_in_front_by_its_link_to_what_crossed_the_rim(monkeypatch):
    frames = _frames()
    surface = screen_insert.Surface.of_plate([(frames[n], QUAD) for n in screen_insert.plate_frames(0, FRAMES - 1)])
    n = FRAMES // 2
    shape = _on_grid(_object(n), surface.grid)
    head = ndimage.binary_erosion(shape & ~screen_insert._rim_mask(surface.grid), iterations=2)
    front, confidence = surface._occluder(frames[n], QUAD)
    # Its head over the screen is in front, told from it by the weaker change alone: dark on dark.
    assert front[head].mean() > 0.95
    assert confidence is not None and confidence < screen_insert.LOW_FRONT_CONFIDENCE
    # The ring of weaker change round the strong edges it crossed is their blur, never grown into: the mask keeps
    # within the object's own blur everywhere.
    assert not (front & ~ndimage.binary_dilation(shape, iterations=3)).any()
    # Without the weaker change grown into, the object is cut at the screen's edge: the defect.
    monkeypatch.setattr(screen_insert, "WEAK_CHANGE_MIN", 255.0)
    assert surface.occluded(frames[n], QUAD)[head].mean() < 0.1


def test_what_stands_in_front_when_the_insert_comes_in_is_no_part_of_its_clean_surface():
    assert screen_insert.plate_frames(0, FRAMES - 1) == (0, 2, 4, 6, 7, 9, 11)
    assert screen_insert.plate_frames(5, 5) == (5,) and screen_insert.plate_frames(3, 4) == (3, 4)
    # Its head already over the screen on the frame the insert comes in on.
    start = 260
    frames = _frames(start)
    first = screen_insert.Surface.of(frames[0], QUAD)
    plate = screen_insert.Surface.of_plate([(frames[n], QUAD) for n in screen_insert.plate_frames(0, FRAMES - 1)])
    inside = ~screen_insert._rim_mask(plate.grid)
    # On frame 1 the object still stands where it stood on frame 0: that part never differs from frame 0 ...
    stayed = ndimage.binary_erosion(_on_grid(_object(0, start) & _object(1, start), plate.grid) & inside, iterations=2)
    assert stayed.sum() > 200
    assert first.occluded(frames[1], QUAD)[stayed].mean() < 0.1
    # ... and differs from the clean surface, on that frame and on the frame the insert comes in on alike.
    assert plate.occluded(frames[1], QUAD)[stayed].mean() > 0.95
    standing = ndimage.binary_erosion(_on_grid(_object(0, start), plate.grid) & inside, iterations=2)
    assert plate.occluded(frames[0], QUAD)[standing].mean() > 0.95


# ── The matte and the gate ────────────────────────────────────────────────


def test_a_dark_object_crossing_a_dark_screen_stays_in_front_of_an_unkeyed_insert_on_every_frame():
    layers, audits, found, report = _composed(_frames())
    # Drawn on none of its pixels, on any frame: the mask the gate reads is the one that mattes.
    worst = [_drawn_on_object(layer, n) for n, layer in enumerate(layers)]
    assert max(worst) <= screen_insert.DRAWN_MIN_ALPHA, worst
    assert all(audit["covered"] == 0.0 for audit in audits[KEY]["frames"].values())
    # Never drawn over: its mask's doubt is the one finding - in front by its link to what crossed the
    # screen's edge, dark on dark, on more than a few of its frames.
    assert [finding.kind for finding, _proof in found] == [render_checks.INSERT_GHOST], [finding.message for finding, _proof in found]
    sure = report["foreground"]["confidence"]
    assert sure["lowest"] < screen_insert.LOW_FRONT_CONFIDENCE and sure["low"] >= 1 and sure["frames"] >= FRAMES - 2
    assert sure["lowest_at"] == round(sure["lowest_frame"] / FPS, 3) and sure["ghost"] >= 1
    [line] = [m.line() for m in insert_checks.classify(report, {render_checks.INSERT_GHOST}) if "what stands in front covers" in m.text]
    assert line.startswith("finding (insert_ghost): ")
    assert "kept in front of the insert" in line and "sure at its lowest" in line and "(dark on dark)" in line


def test_the_same_object_with_the_insert_forced_over_it_fails_insert_over_foreground_with_its_frame_and_crop(monkeypatch, tmp_path):
    # The matte rule broken: the insert drawn whole whatever the mask says.
    monkeypatch.setattr(screen_insert, "in_front", lambda share, core: np.ones_like(share))
    layers, _audits, found, report = _composed(_frames(), tmp_path / "evidence")
    assert max(_drawn_on_object(layer, n) for n, layer in enumerate(layers)) > 0.9
    kinds = {finding.kind: (finding, proof) for finding, proof in found}
    finding, proof = kinds[render_checks.INSERT_FOREGROUND]
    assert finding.severity == render_checks.FAIL and finding.scene_id == "s1"
    assert finding.message.startswith(f"insert over foreground at t=0:00.{round(100 * finding.data['worst_at']):02d}: {LABEL} is drawn over")
    assert "past 0.5%; its worst stretch kept at 100%)" in finding.message
    assert finding.data["worst"] > screen_insert.MAX_COVERED_SHARE == 0.005 and finding.data["threshold"] == 0.005
    assert finding.data["frames"] >= FRAMES - 2 and "front_confidence" in finding.data
    # The frame that shows it, its worst stretch at 100 %: the insert's dashboard where the object stands.
    assert proof and Path(proof).is_file() and proof.endswith(".jpg")
    crop = report["foreground_crop"]
    assert crop["frame"] == finding.data["worst_frame"] and Path(crop["path"]).is_file()
    with Image.open(crop["path"]) as shown:
        assert shown.format == "PNG" and min(shown.size) >= screen_insert.HALO_CROP_MIN_PX
        assert float(np.asarray(shown.convert("L"), dtype=np.float32).mean()) > FUR + 10
    # The render stores it with the insert's other crops, deleted with the render.
    crop["file_id"] = "file-over"
    assert "file-over" in render_checks.insert_crop_ids(render_checks.Checks(inserts=[report]).as_dict())
    # Past half a percent on a single frame is enough: no run of frames is asked.
    one = {KEY: {"frames": {"3": {"covered": 0.0051}, "4": {"covered": 0.0}}, "evidence": {}}}
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0)}
    [(single, _proof)] = insert_checks.findings(one, placed, fps=FPS)
    assert single.kind == render_checks.INSERT_FOREGROUND and single.data["frames"] == 1
    assert not insert_checks.findings({KEY: {"frames": {"3": {"covered": 0.0049}}, "evidence": {}}}, placed, fps=FPS)


def test_a_keyed_subject_the_key_reads_part_key_is_never_seen_through_the_insert():
    # A green surface, a green-lit grey subject across its bottom edge: the key reads its colour about a quarter key.
    picture = Image.fromarray((np.random.default_rng(2).random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in QUAD], fill=(0, 177, 64))
    draw.rectangle((280, 190, 360, H), fill=(71, 130, 87))
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(1.0)))
    insert = screen_insert.keyed_on(_insert(key=screen_insert.KeySpec("green", 0.3, 1.5)), frame)
    # The same frame drawn with a white source and a black one: where the two layers agree, the insert is not seen.
    white, black = (screen_insert.compose(insert, Image.new("RGB", (960, 692), (v, v, v)), (W, H), 0, footage=frame) for v in (255, 0))
    x0, y0, x1, y1 = white.warped.box
    inside = screen_insert._coverage(QUAD, white.warped.box) > 0.5
    subject = np.zeros((H, W), dtype=bool)
    subject[190:, 280:361] = True
    subject = subject[y0:y1, x0:x1] & inside
    matte = screens.key_matte(frame[y0:y1, x0:x1].astype(np.float32), insert.key.key(), insert.key.matte_tolerance())
    core = ndimage.binary_erosion(subject, iterations=4)
    assert 0.15 < float(np.median(matte[core])) < 0.45, "the key reads the subject part key"
    seen = np.abs(np.asarray(white.warped.image, dtype=np.float32) - np.asarray(black.warped.image, dtype=np.float32))[..., :3].max(axis=-1) / 255.0
    assert float(seen[core].max()) <= screen_insert.DRAWN_MIN_ALPHA, "never seen through the insert"
    # Its edge keeps the key's share of each pixel: the insert blends into it there, and shows whole on the surface.
    ring = subject & ~ndimage.binary_erosion(subject, iterations=2)
    assert ((seen[ring] > 0.1) & (seen[ring] < 0.9)).any()
    clear = inside & ~ndimage.binary_dilation(subject, iterations=6)
    assert float(np.median(seen[clear])) > 0.95
    assert white.audit.covered == 0.0
    # Its confidence says the key reads it part key, not clear of the surface.
    assert white.audit.front_confidence is not None and white.audit.front_confidence < screen_insert.LOW_FRONT_CONFIDENCE


# ── End to end: the frame server ──────────────────────────────────────────


def _footage(tmp_path: Path, frames: list[np.ndarray]) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is missing")
    for n, frame in enumerate(frames):
        Image.fromarray(frame).save(tmp_path / f"f{n:03d}.png")
    out = tmp_path / "footage.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(int(FPS)), "-i", str(tmp_path / "f%03d.png")]
    subprocess.run(  # noqa: S603
        [*command, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv444p", str(out)],
        check=True,
        timeout=120,
    )
    return out


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.answer: dict = {}

    async def fulfill(self, **kwargs):
        self.answer = kwargs


def test_the_frame_server_reads_the_plate_off_the_footage_and_keeps_the_object_in_front_on_every_frame(tmp_path):
    frames = _frames()
    footage = _footage(tmp_path, frames)
    picture = tmp_path / "ui.png"
    _ui().save(picture)
    insert = dataclasses.replace(_insert(str(footage)), path=str(picture))
    # The plate is what its frames are drawn by, and what a range worker is handed.
    assert compositor.insert_drawn_by(insert, None)["plate"] == list(insert.plate)
    assert compositor.insert_drawn_by(dataclasses.replace(insert, plate=()), None).get("plate") is None
    assets = compositor.CompositorAssets(sources={"clip/s1": compositor.FrameSource(str(footage), FPS)}, inserts={KEY: insert})
    assert render_ranges._manifest(assets, lambda path: path)["inserts"][KEY]["plate"] == list(insert.plate)

    async def serve() -> tuple[list[np.ndarray], dict]:
        server = compositor.FrameServer(assets, "<html></html>", max_open=3)
        shown = []
        try:
            for n in range(FRAMES):
                server.start_frame()
                route = _Route(f"{compositor.ORIGIN}/{KEY}/0.png?w={W}&h={H}&f={n}&n={n}")
                await server.handle(route)
                assert route.answer["status"] == 200
                with Image.open(io.BytesIO(route.answer["body"])) as layer:
                    shown.append(np.asarray(layer.convert("RGBA"), dtype=np.float32)[..., 3] / 255.0)
            return shown, server.take_audits(0, FRAMES)
        finally:
            await server.close()

    shown, audits = asyncio.run(serve())
    x0, y0, x1, y1 = screen_insert.bbox(list(insert.box), W, H)
    inside = screen_insert._coverage(QUAD, (x0, y0, x1, y1)) > 0.5
    for n, alpha in enumerate(shown):
        core = ndimage.binary_erosion(_object(n)[y0:y1, x0:x1] & inside, iterations=2)
        assert not core.any() or float(alpha[core].max()) <= screen_insert.DRAWN_MIN_ALPHA, n
    assert all(audit["covered"] == 0.0 for audit in audits[KEY]["frames"].values())
