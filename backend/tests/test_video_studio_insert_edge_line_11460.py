"""A keyed screen insert's quad edge never drawn over what stands in front (#11460).

A presenter standing in front of a green screen's side crosses the insert's
quad edge. Read inside the quad alone, the mask of what stands in front ended
at that edge: its feather and its core took the quad's edge for the subject's
own, and the insert was drawn at part of its opacity along it, its antialiased
pixels included, as a thin light line over the presenter for the whole scene.
No gate saw it. What is proven:

* ``compose`` draws neither the quad's edge nor its antialiased pixels over a
  subject crossing it, and ``FrameAudit.edge_line`` reads none;
* the compositing before the fix (the mask kept inside the quad) fails the new
  gate, with its frame and its crop;
* the gate reads 0 on an insert no subject crosses, its drawing unchanged;
* ``findings`` turns a frame past ``MAX_EDGE_LINE_SHARE`` into an
  ``insert_foreground`` finding on its worst frame, and the sweep frames it.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import insert_checks, render_checks, screen_insert

W, H = 640, 360
FPS = 24.0
KEY = "insert/s1.0"
LABEL = "Scene s1: its screen insert 0"
#: The screen's corners (pixels), its right edge off the pixel grid so the quad's edge is antialiased.
QUAD = np.array([(200.0, 80.0), (440.4, 80.0), (440.4, 253.0), (200.0, 253.0)], dtype=float)
GREEN = (0, 177, 64)
BEZEL = (40, 42, 46)
SUBJECT = (150, 110, 95)
#: The presenter: a body standing across the screen's right edge.
STANDING = (420, 120, 500, H)


def _footage(subject: bool = True) -> np.ndarray:
    picture = Image.fromarray((np.random.default_rng(11460).random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = QUAD.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (QUAD - centre) * 1.04], fill=BEZEL)
    draw.polygon([tuple(p) for p in QUAD], fill=GREEN)
    if subject:
        draw.rectangle(STANDING, fill=SUBJECT)
    return np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8)))


def _insert(frame: np.ndarray) -> screen_insert.InsertSource:
    quad = tuple((float(x) / W, float(y) / H) for x, y in QUAD)
    source = screen_insert.InsertSource(
        "p.png",
        "image",
        quad,
        footage=screen_insert.Footage("f", "video", FPS, "clip/s1"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        plate=screen_insert.plate_frames(0, 0),
        label=LABEL,
        key=screen_insert.KeySpec("green", 0.3, 1.5),
    )
    return screen_insert.keyed_on(source, frame)


def _drawn(frame: np.ndarray) -> tuple[screen_insert.Layer, np.ndarray]:
    """The frame drawn with a white source, and where the insert shows on it: the white and black layers' difference."""
    insert = _insert(frame)
    white, black = (
        screen_insert.compose(insert, Image.new("RGB", (960, 692), (v, v, v)), (W, H), 0, footage=frame, evidence=True)
        for v in (255, 0)
    )
    assert white is not None and black is not None
    seen = (
        np.abs(np.asarray(white.warped.image, dtype=np.float32) - np.asarray(black.warped.image, dtype=np.float32))[
            ..., :3
        ].max(axis=-1)
        / 255.0
    )
    return white, seen


def _subject_core(box: tuple[int, int, int, int]) -> np.ndarray:
    """The presenter's pixels in the box along the quad's right edge, its own blurred edge and the quad's corners
    left out: the vertical line of the report."""
    x0, y0, x1, y1 = box
    mask = np.zeros((H, W), dtype=bool)
    left, top, right, _bottom = STANDING
    mask[top + 3 : int(QUAD[2][1]) - 3, left + 3 : right - 2] = True
    return mask[y0:y1, x0:x1]


def _pre_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compositing before #11460: the mask of what stands in front kept inside the quad."""
    monkeypatch.setattr(screen_insert, "front_past_quad", lambda foreground, nonkey, inside, grow: foreground)


def test_the_quad_edge_is_never_drawn_over_a_subject_crossing_it():
    layer, seen = _drawn(_footage())
    core = _subject_core(layer.warped.box)
    assert core.any()
    assert float(seen[core].max()) <= screen_insert.DRAWN_MIN_ALPHA, "no edge pixel drawn over the presenter"
    assert not layer.audit.draws_edge_line
    assert (layer.audit.edge_line or 0.0) <= screen_insert.MAX_EDGE_LINE_SHARE
    assert not layer.audit.covers_foreground


def test_the_compositing_before_the_fix_fails_the_gate(monkeypatch):
    _pre_fix(monkeypatch)
    layer, seen = _drawn(_footage())
    core = _subject_core(layer.warped.box)
    assert float(seen[core].max()) > screen_insert.DRAWN_MIN_ALPHA, "the line the fix removes"
    assert layer.audit.draws_edge_line
    assert layer.audit.failing
    # Its evidence: the frame marked and the crop around the line, at 100 %.
    assert layer.evidence is not None and layer.foreground_crop is not None


def test_an_insert_no_subject_crosses_reads_no_edge_line_and_is_drawn_the_same(monkeypatch):
    frame = _footage(subject=False)
    layer, seen = _drawn(frame)
    assert layer.audit.edge_line in (None, 0.0)
    _pre_fix(monkeypatch)
    before, seen_before = _drawn(frame)
    assert np.array_equal(np.asarray(layer.warped.image), np.asarray(before.warped.image))
    assert np.array_equal(seen, seen_before)


def test_a_frame_past_the_edge_line_share_is_an_insert_foreground_finding():
    placed = {KEY: insert_checks.Placed(KEY, "s1", LABEL, 0.0, 1.0)}
    over = screen_insert.MAX_EDGE_LINE_SHARE * 3
    audits = {KEY: {"frames": {"3": {"edge_line": over, "covered": 0.0}, "4": {"edge_line": 0.0}}, "evidence": {}}}
    [(finding, _proof)] = insert_checks.findings(audits, placed, fps=FPS)
    assert finding.kind == render_checks.INSERT_FOREGROUND
    assert finding.data["edge_line"] is True and finding.data["worst_frame"] == 3 and finding.data["frames"] == 1
    assert insert_checks.failing_frames({int(n): audit for n, audit in audits[KEY]["frames"].items()}) == {
        3: ["covered"]
    }
    under = {KEY: {"frames": {"3": {"edge_line": screen_insert.MAX_EDGE_LINE_SHARE / 2}}, "evidence": {}}}
    assert not insert_checks.findings(under, placed, fps=FPS)
