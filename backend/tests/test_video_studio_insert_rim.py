"""The key's residue on the edge of what stands in front: drawn as the insert, and read as a grey rim.

Video b56997a2 rev 138 (render 0f47dcc0, harness at DRAWING_VERSION 9): Marguerite's head over the OpenAEV dashboard
was drawn with a neutral grey rim 4-12 px wide on the insert's side of its edge, the halo gate at 0.0 %. On synthetic
1080p footage, every frame composed through ``screen_insert.compose`` as the frame server composes it - a static keyed
insert on its fill and its plate's surface, read on the frame it comes in on (``compositor._matte_of``); a keyed one
read frame by frame on its matte's window - and read back as the render's findings and report (``insert_checks``):

* F57 a head with an 8 px dark-brown rim crossing a bright blue panel over 8 frames: before the fix
  (``key_residue`` left out) the band 2-8 px past it reads a mean dE over 25 against the insert, ``rim`` over 0.3,
  ``insert_rim`` fails with its one action and its crop and the halo reads nothing; after it, dE under 10, ``rim``
  under 0.05, no finding. The matte read frame by frame, its window holding the key in front at the clip's ends,
  the same;
* F58 the same head over a dark insert: ``rim`` under 0.05 before and after - the residue does not show there;
* F59 the key's hue left on the head's edge (a matte read hard, no clamp, the edge despill off) fails
  ``insert_halo``, never ``insert_rim``;
* the gate on ``MIN_FAILING_FRAMES`` frames past 15 %, its one action - the insert's light lowered where that light
  made the rim grey, else the shot made again, ``not a setting`` when another light left the same rim -, the report's
  ``rim``, ``rim_parts`` and ``rim_crop``, the insert's facts, the evidence kept and the drawing version moved.
"""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

from app.services.video_studio import compositor, insert_checks, insert_frames, render_checks, screen_insert, screens

W, H = 1920, 1080
GREEN = (32, 255, 30)
#: The screen's true corners (pixels): a monitor seen a little from the side.
TRUE = np.array([(560, 220), (1360, 236), (1352, 810), (568, 796)], dtype=float)
FRAMES = 8
FPS = 24.0
KEY = "insert/demo-openaev.0"
LABEL = "Scene demo-openaev: its screen insert 0"
#: The insert: a bright blue panel (F57), a dark one (F58).
BLUE, DARK = (40, 120, 235), (22, 26, 34)
#: The head: skin inside an 8 px dark-brown rim (its hair), 130 px in radius, 30 px further across the panel each frame.
SKIN, HAIR, HAIR_PX = (196, 146, 118), (58, 38, 28), 8
#: The camera's softness, a Gaussian of this many pixels: a 1080p shot's edges are never sharper.
SOFT = 2.5


def _head(n: int) -> tuple[int, int, int]:
    return 760 + 30 * n, 600, 130


@functools.cache
def _footage(n: int | None) -> tuple[np.ndarray, np.ndarray]:
    """Footage frame *n*: a textured room, a dark bezel, a flat green screen and the head over it, its shoulders below
    it, softened as a camera shows them, with the encoder's noise; and where the head and its shoulders are (boolean).
    *n* ``None``: the screen alone - the frame the insert comes in on."""
    rng = np.random.default_rng((n or 0) + 100)
    picture = Image.fromarray((rng.random((H, W, 3)) * 30 + 90).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    centre = TRUE.mean(axis=0)
    draw.polygon([tuple(p) for p in centre + (TRUE - centre) * 1.05], fill=(18, 20, 24))
    draw.polygon([tuple(p) for p in TRUE], fill=GREEN)
    where = Image.new("L", (W, H), 0)
    if n is not None:
        x, y, r = _head(n)
        shoulders = (x - 2 * r, y + int(1.2 * r), x + 2 * r, H)
        draw.rectangle(shoulders, fill=(40, 44, 60))
        draw.ellipse((x - r, y - r, x + r, y + int(1.3 * r)), fill=HAIR)
        draw.ellipse((x - r + HAIR_PX, y - r + HAIR_PX, x + r - HAIR_PX, y + int(1.3 * r) - HAIR_PX), fill=SKIN)
        marked = ImageDraw.Draw(where)
        marked.ellipse((x - r, y - r, x + r, y + int(1.3 * r)), fill=255)
        marked.rectangle(shoulders, fill=255)
    pixels = np.asarray(picture.filter(ImageFilter.GaussianBlur(SOFT)), dtype=np.float32)
    pixels += rng.normal(0.0, 3.0, pixels.shape).astype(np.float32)
    return np.clip(pixels, 0, 255).astype(np.uint8), np.asarray(where) > 127


def _insert(**changes) -> screen_insert.InsertSource:
    quad = tuple((float(x) / W, float(y) / H) for x, y in TRUE)
    insert = screen_insert.InsertSource(
        "panel.png",
        "image",
        quad,
        key=screen_insert.KeySpec("green"),
        footage=screen_insert.Footage("clip", "video", FPS, "clip/demo-openaev"),
        label=LABEL,
    )
    return dataclasses.replace(insert, **changes)


@dataclasses.dataclass
class Drawn:
    """An insert's frames drawn: their audits as a render keeps them, its layers, and the frames composited."""

    insert: screen_insert.InsertSource
    audits: dict
    layers: list
    composed: list


def _drawn(colour: tuple[int, int, int], *, static: bool = True, insert: screen_insert.InsertSource | None = None, tmp: Path | None = None) -> tuple[Drawn, list[np.ndarray]]:
    """The 8 frames of the head crossing the panel, *colour* the insert drawn, composed as the frame server composes
    them: *static*, on the fill and the plate read on the frame the insert comes in on; else keyed frame by frame on
    its matte's window. With *tmp*, the evidence a failing frame keeps is written there as the compositor keeps it."""
    clean, _where = _footage(None)
    footage = [_footage(n) for n in range(FRAMES)]
    frames = [pixels for pixels, _mask in footage]
    insert = screen_insert.keyed_on(insert or _insert(), clean)
    picture = Image.new("RGB", (1600, 1150), colour)
    surface = fill = None
    if static:
        surface = screen_insert.Surface.of_plate([(clean, TRUE)])
        fill = screen_insert.static_fill(insert, clean, (W, H))
        assert fill is not None
    matting = screen_insert.Matting()
    audits: dict = {"frames": {}, "evidence": {}}
    layers, composed = [], []
    for n, frame in enumerate(frames):
        ahead = [(n + k, frames[n + k]) for k in (1, 2) if n + k < len(frames)]
        layer = screen_insert.compose(
            insert, picture, (W, H), n, footage=frame, surface=surface, fill=fill, state=matting, footage_ahead=ahead, evidence=True
        )
        assert layer is not None
        layers.append(layer)
        audits["frames"][str(n)] = layer.audit.as_dict()
        x0, y0, x1, y1 = layer.warped.box
        image = np.asarray(layer.warped.image, dtype=np.float32)
        alpha = image[..., 3:4] / 255.0
        composed.append(image[..., :3] * alpha + frame[y0:y1, x0:x1].astype(np.float32) * (1.0 - alpha))
        if tmp is not None and layer.audit.rimmed:
            for kind, picture_kept in (("rim", layer.evidence), ("rim_crop", layer.rim_crop)):
                assert picture_kept is not None, kind
                path = tmp / f"{kind}-{n}.png"
                picture_kept.save(path)
                audits["evidence"].setdefault(kind, []).append({"frame": n, "badness": layer.audit.rim, "path": str(path)})
    return Drawn(insert, audits, layers, composed), [mask for _pixels, mask in footage]


def _lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0-255) as CIELAB (D65)."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = c @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _band_de(drawn: Drawn, heads: list[np.ndarray], colour: tuple[int, int, int]) -> float:
    """The mean dE (CIE76) against the insert's colour of the composited pixels 2-8 px past the head, inside the panel,
    over every frame: what the audience sees on the insert's side of its edge."""
    values = []
    for layer, composed, head in zip(drawn.layers, drawn.composed, heads, strict=True):
        x0, y0, x1, y1 = layer.warped.box
        inside = ndimage.binary_erosion(screen_insert._coverage(TRUE, (x0, y0, x1, y1)) > 0.5, iterations=12)
        past = ndimage.distance_transform_edt(~head[y0:y1, x0:x1])
        band = inside & (past > 2.0) & (past <= 8.0)
        values.append(np.linalg.norm(_lab(composed[band]) - _lab(np.array(colour, dtype=np.float64)), axis=-1))
    return float(np.concatenate(values).mean())


def _placed(insert: screen_insert.InsertSource, **changes) -> dict[str, insert_checks.Placed]:
    where = insert_checks.Placed(KEY, "demo-openaev", insert.label, 0.0, FRAMES / FPS, light=insert.light, keyed=True)
    return {KEY: dataclasses.replace(where, **changes)}


def _read(drawn: Drawn, **changes) -> tuple[dict, list]:
    placed = _placed(drawn.insert, **changes)
    found = [finding for finding, _proof in insert_checks.findings({KEY: drawn.audits}, placed, fps=FPS)]
    [report] = insert_checks.reports({KEY: drawn.audits}, placed, fps=FPS)
    return report, found


@pytest.fixture
def before(monkeypatch):
    """The drawing without the residue key: the key's residue left as the clamp draws it, and no pocket of it
    keyed out (``key_out_pockets``, which keys out what the frame's own key reads whole and draws the insert there)."""
    monkeypatch.setattr(screen_insert, "key_residue", lambda colours, *_args, **_kwargs: colours)
    monkeypatch.setattr(screen_insert, "key_out_pockets", lambda visible, foreground, *_args: (visible, foreground))


# ── F57: a dark-edged head over a bright panel ─────────────────────────────


def test_f57_before_the_fix_the_head_carries_a_grey_rim_that_fails_insert_rim_and_no_halo(before, tmp_path):
    drawn, heads = _drawn(BLUE, tmp=tmp_path)
    assert _band_de(drawn, heads, BLUE) > 25.0
    report, found = _read(drawn)
    assert report["rim"] > 0.3, report["rim"]
    # A grey band is no hue outlier: the halo gate reads nothing of it.
    assert report["halo"] is not None and report["halo"] <= screen_insert.MAX_HALO_SHARE
    kinds = {finding.kind: finding for finding in found}
    assert render_checks.INSERT_HALO not in kinds
    rim = kinds[render_checks.INSERT_RIM]
    assert rim.severity == render_checks.FAIL and rim.scene_id == "demo-openaev"
    assert rim.data["frames"] >= insert_checks.MIN_FAILING_FRAMES
    assert rim.message.startswith(f"insert rim at t={insert_checks.clock(rim.data['worst_at'])}: {LABEL}: grey on ")
    assert "of the band 2-12 px past what stands in front" in rim.message and "its worst stretch kept at 100%" in rim.message
    # Its gate, its value against its threshold, and its one action: the footage's own edge, the shot made again.
    assert rim.data["gate"] == "rim" and rim.data["threshold"] == screen_insert.MAX_RIM_SHARE == 0.15
    assert rim.data["value"] == rim.data["worst"] == report["rim"]
    assert rim.data["action"]["kind"] == insert_checks.REGENERATE_SHOT and rim.fix == rim.data["action"]["text"]
    assert rim.message.endswith(f" - {rim.fix}")
    # Its worst stretch at 100 %, beside the report's numbers.
    assert report["rim_crop"]["path"].endswith(".png") and report["rim_parts"]["edge_px"] > 100
    assert report["verdicts"]["rim"] is False
    assert render_checks.INSERT_RIM in render_checks.PICTURE_KINDS


def test_f57_after_the_fix_the_band_reads_as_the_insert_behind_a_soft_edge():
    drawn, heads = _drawn(BLUE)
    assert _band_de(drawn, heads, BLUE) < 10.0
    report, found = _read(drawn)
    assert report["rim"] is not None and report["rim"] < 0.05, report["rim"]
    assert not {render_checks.INSERT_RIM, render_checks.INSERT_HALO} & {finding.kind for finding in found}, [f.message for f in found]
    assert report["verdicts"]["rim"] is True
    # Nothing of the head is drawn over: its skin keeps its colour, its dark rim stays dark.
    middle, layer = drawn.composed[FRAMES // 2], drawn.layers[FRAMES // 2]
    x0, y0, _x1, _y1 = layer.warped.box
    x, y, r = _head(FRAMES // 2)
    assert np.abs(middle[y - y0, x - x0] - np.array(SKIN)).max() < 20
    assert middle[y - y0, x - r + HAIR_PX // 2 - x0].max() < 90
    assert all(audit["covered"] == 0.0 for audit in drawn.audits["frames"].values())


def test_f57_keyed_frame_by_frame_the_key_its_window_holds_in_front_is_drawn_as_the_insert(monkeypatch):
    """The matte read frame by frame - a tracked insert's - keeps the lower median of its window: at the clip's ends the
    frames ahead of the head hold the key in front of it, the unmix capped by that matte leaves the key in the pixel and
    the clamp turns it grey. Drawn at the frame's own key share, it reads as the insert."""
    with monkeypatch.context() as patched:
        patched.setattr(screen_insert, "key_residue", lambda colours, *_args, **_kwargs: colours)
        left, heads = _drawn(BLUE, static=False)
    assert max(audit["rim"] or 0.0 for audit in left.audits["frames"].values()) > 0.2
    drawn, heads = _drawn(BLUE, static=False)
    assert _band_de(drawn, heads, BLUE) < 10.0 < _band_de(left, heads, BLUE)
    report, found = _read(drawn)
    assert report["rim"] < 0.05 and render_checks.INSERT_RIM not in {finding.kind for finding in found}


# ── F58: the same head over a dark insert ──────────────────────────────────


@pytest.mark.parametrize("fixed", [False, True])
def test_f58_the_same_head_over_a_dark_insert_reads_no_rim(fixed, monkeypatch):
    if not fixed:
        monkeypatch.setattr(screen_insert, "key_residue", lambda colours, *_args, **_kwargs: colours)
    drawn, _heads = _drawn(DARK)
    report, found = _read(drawn)
    assert report["rim"] is not None and report["rim"] < 0.05, report["rim"]
    assert render_checks.INSERT_RIM not in {finding.kind for finding in found}


# ── F59: the key's hue on the edge is the halo's ───────────────────────────


def test_f59_the_key_s_hue_left_on_the_head_s_edge_fails_insert_halo_not_insert_rim(monkeypatch):
    """A matte read hard and no clamp, the edge despill off (``test_a_hard_edged_matte_fails_the_render``'s): the key's
    green stays on the head's edge strands - hue outliers, never grey."""
    soft = screens.key_matte
    monkeypatch.setattr(screens, "key_matte", lambda rgb, key, tolerance=None: (soft(rgb, key, tolerance) > 0.5).astype(np.float32))
    monkeypatch.setattr(screens, "despill_clamp", lambda rgb, key, where=None: rgb)
    insert = _insert(despill=screen_insert.Despill(edge=False), key=screen_insert.KeySpec("green", feather=0.0))
    drawn, _heads = _drawn(BLUE, static=False, insert=insert)
    report, found = _read(drawn)
    kinds = {finding.kind for finding in found}
    assert render_checks.INSERT_HALO in kinds and report["halo"] > screen_insert.MAX_HALO_SHARE
    assert render_checks.INSERT_RIM not in kinds and report["rim"] < screen_insert.MAX_RIM_SHARE, report["rim"]


# ── The gate, its action, its report and its facts ─────────────────────────


def _audits(rims: list[float | None], *, light: float = 0.0) -> dict:
    return {"frames": {str(n): {"rim": rim, "rim_edge": 640, "rim_light": light if rim else None} for n, rim in enumerate(rims)}, "evidence": {}}


def _rim_finding(audits: dict, **changes) -> render_checks.Finding | None:
    placed = _placed(_insert(), **changes)
    found = [finding for finding, _proof in insert_checks.findings({KEY: audits}, placed, fps=FPS)]
    return next((finding for finding in found if finding.kind == render_checks.INSERT_RIM), None)


def test_the_rim_fails_on_min_failing_frames_frames_past_15_percent():
    assert insert_checks.GATE_OF[render_checks.INSERT_RIM] == "rim"
    assert _rim_finding(_audits([0.4] * 4 + [0.1] * 6)) is None
    assert _rim_finding(_audits([0.15] * 10)) is None
    finding = _rim_finding(_audits([0.4] * insert_checks.MIN_FAILING_FRAMES + [0.0] * 5))
    assert finding is not None and finding.data["frames"] == insert_checks.MIN_FAILING_FRAMES and finding.data["edge_px"] == 640
    # An insert drawn on fewer frames fails on all of them.
    assert _rim_finding(_audits([0.3, 0.3, 0.3])) is not None
    assert insert_checks.verdicts({n: {"rim": 0.4} for n in range(5)}) == {"rim": False, "flicker": True}


def test_the_rim_s_one_action_is_the_light_where_the_light_made_it_else_the_shot():
    lit = _rim_finding(_audits([0.4] * 6, light=0.8), light=0.6)
    assert lit.data["action"]["kind"] == insert_checks.LOWER_LIGHT and "lower its light (light: 0.6 now" in lit.message
    assert "80% of it where the insert's light took its colour" in lit.message
    footage = _rim_finding(_audits([0.4] * 6, light=0.1), light=0.6)
    assert footage.data["action"]["kind"] == insert_checks.REGENERATE_SHOT
    assert "generate the shot again with what stands in front lit apart from the surface" in footage.fix
    # The same rim on an earlier drawing at another light: the light is not what leaves it.
    earlier = [{"render_id": "r1", "measures": {"rim": 0.4}, "key_settings": None, "light": 0.2}]
    frames = {n: audit for n, audit in enumerate(_audits([0.4] * 6, light=0.8)["frames"].values())}
    where = _placed(_insert(), light=0.6)[KEY]
    [again] = [f for f, _p in insert_checks._compositing_findings(KEY, where, frames, {}, lambda n: n / FPS, earlier) if f.kind == render_checks.INSERT_RIM]
    assert again.data["action"]["kind"] == insert_checks.REGENERATE_SHOT and again.fix.startswith("not a setting: foreground - ")
    assert again.data["unchanged"]["render_id"] == "r1"


def test_the_report_and_the_facts_say_the_rim():
    audits = _audits([0.02, 0.31, 0.2, 0.0], light=0.25)
    audits["evidence"]["rim_crop"] = [{"frame": 1, "badness": 0.31, "path": "evidence/rim.png"}]
    placed = _placed(_insert())
    [report] = insert_checks.reports({KEY: audits}, placed, fps=FPS)
    assert report["rim"] == 0.31 and report["measures"]["rim"] == 0.31
    assert report["rim_parts"] == {"frame": 1, "at": round(1 / FPS, 3), "edge_px": 640, "light": 0.25}
    assert report["rim_crop"] == {"frame": 1, "at": round(1 / FPS, 3), "path": "evidence/rim.png"}
    report["rim_crop"]["file_id"] = "file-rim"
    said = "grey rim 31.0% of the band 2-12 px past what stands in front (640 px of its edge), where the insert is not; 25% of it where the insert's light took its colour; its worst stretch at 100%: file `file-rim`"
    assert f"fact: {said}" in [m.line() for m in insert_checks.classify(report)]
    assert f"finding (insert_rim): {said}" in [m.line() for m in insert_checks.classify(report, {render_checks.INSERT_RIM})]
    # The crop is one of the render's files, stored and cleaned up with the others.
    assert "rim_crop" in render_checks.INSERT_STRIPS
    assert render_checks.insert_crop_ids({"inserts": [report]}) == ["file-rim"]


def test_the_audit_keeps_the_rim_and_a_failing_frame_keeps_its_crop():
    audit = screen_insert.FrameAudit(rim=0.2, rim_edge=300, rim_light=0.0)
    record = audit.as_dict()
    assert (record["rim"], record["rim_edge"], record["rim_light"]) == (0.2, 300, 0.0)
    assert screen_insert.FrameAudit.from_dict(record).rimmed and audit.failing
    assert not screen_insert.FrameAudit(rim=0.15).rimmed
    assert insert_frames.EVIDENCE["rim_crop"] == "png"
    assert set(screen_insert.Layer(screen_insert.Warped((0, 0, 1, 1), None), None).pictures()) >= {"rim_crop"}


async def test_the_compositor_keeps_a_rimmed_frame_s_proof_and_its_crop(tmp_path):
    server = compositor.FrameServer.__new__(compositor.FrameServer)
    server.audits, server.evidence, server.evidence_dir = {}, {}, tmp_path
    proof, crop = Image.new("RGB", (32, 32)), Image.new("RGB", (16, 16))
    await server._audited(KEY, 7, screen_insert.FrameAudit(rim=0.4), {"proof": proof, "rim_crop": crop})
    assert server.evidence[(KEY, "rim")][:2] == (0.4, 7) and server.evidence[(KEY, "rim_crop")][:2] == (0.4, 7)
    assert Path(server.evidence[(KEY, "rim_crop")][2]).is_file()


def test_the_drawing_version_moved_so_no_segment_drawn_before_is_joined():
    assert screen_insert.DRAWING_VERSION >= 10
    assert compositor._insert_inputs(_insert(), compositor.CompositorAssets(), [])["drawing"] == screen_insert.DRAWING_VERSION
