"""A logo drawn onto an AI shot reads as part of its surface, or fails as "logo not integrated".

Synthetic footage only: a quilted surface (diamond quilting, a weave, the
broad light of a curved flank) moving, bending and growing over 8 seconds.

* stamped the old way - the asset's own pixels pasted flat, its size and
  shape kept, moved with the surface's centre only - the logo fails the gate
  on its shading, naming the frame;
* warped onto the surface and drawn into its light and relief
  (``logo_integration.blend``) the same logo passes, every measure read;
* a logo moving rigidly over a surface that bends fails on its follow, one
  keeping its size over one that grows on its size;
* a smooth surface lit flat is no defect for a flat logo;
* the take's logo check carries presence, similarity and integration
  (never a status alone), a scene preview and a render list the finding in
  the scene's time, the save quality check refuses it, and
  ``ingredients_quality: "flat"`` accepts it as advice.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scipy import ndimage

from app.services.video_studio import clips, frame_qc, logo_integration, logos, quality, render_checks, runners

W, H = 640, 360
FPS = 12
SECONDS = 8
LOGO_AT = (340.0, 200.0)


def _quilt(width: int = 900, height: int = 500) -> np.ndarray:
    """A navy quilted cloth: diamond stitching puffed between its seams, a weave, the broad light of a curved flank."""
    rng = np.random.default_rng(3)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    wobble = ndimage.gaussian_filter(rng.normal(0, 1, (height, width)), 25) * 120
    a, b = (xs + ys + wobble) / 44.0, (xs - ys + wobble * 0.7) / 44.0
    seam = np.minimum(np.abs(a - np.round(a)), np.abs(b - np.round(b)))
    puff = np.clip(seam * 2, 0, 1) ** 0.6
    weave = ndimage.gaussian_filter(rng.normal(0, 1, (height, width)), 1.2).astype(np.float32) * 14
    blot = ndimage.gaussian_filter(rng.normal(0, 1, (height, width)), 6).astype(np.float32) * 60
    broad = 0.75 + 0.35 * np.sin(xs / width * np.pi)
    shade = (0.45 + 0.55 * puff) * broad * (1 + blot / 255)
    return np.clip(np.array([34, 46, 105], np.float32) * shade[..., None] + weave[..., None], 0, 255)


def _logo() -> Image.Image:
    """A white mark: a disc and four bars, on a transparent ground."""
    mark = Image.new("RGBA", (220, 90), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.ellipse([4, 8, 80, 84], fill=(255, 255, 255, 255))
    for i in range(4):
        draw.rectangle([95 + i * 30, 20, 115 + i * 30, 75], fill=(255, 255, 255, 255))
    return mark


class Surface:
    """The cloth filmed: it slides, rises and falls, grows and shrinks, and a wave runs along it (*bend* pixels)."""

    def __init__(self, *, bend: float = 12.0, grow: float = 0.2, slide: float = 14.0) -> None:
        self.texture, self.logo = _quilt(), _logo()
        self.asset = np.asarray(self.logo, dtype=np.float32)
        self.bend, self.grow, self.slide = bend, grow, slide
        self.ys, self.xs = np.mgrid[0:H, 0:W].astype(np.float32)

    def _motion(self, t: float) -> tuple[float, ...]:
        return -260 + self.slide * t, -60 + 8 * np.sin(t * 0.8), 1.0 + self.grow * np.sin(t * 0.5), self.bend * np.sin(t * 0.9), t * 1.2

    def forward(self, u, v, t: float):
        tx, ty, s, a, phase = self._motion(t)
        return tx + s * u, ty + s * v + a * np.sin(2 * np.pi * u / 300 + phase)

    def _inverse(self, x, y, t: float):
        tx, ty, s, a, phase = self._motion(t)
        u = (x - tx) / s
        return u, (y - ty - a * np.sin(2 * np.pi * u / 300 + phase)) / s

    @staticmethod
    def _sample(image: np.ndarray, u, v) -> np.ndarray:
        return np.stack([ndimage.map_coordinates(image[..., c], [v, u], order=1, mode="nearest") for c in range(image.shape[2])], -1)

    def frame(self, t: float) -> np.ndarray:
        u, v = self._inverse(self.xs, self.ys, t)
        return np.clip(self._sample(self.texture, u, v), 0, 255).astype(np.uint8)

    def warped(self, t: float) -> SimpleNamespace:
        """The asset warped onto the cloth as it is at *t*: a ``screen_insert.Warped``."""
        u, v = self._inverse(self.xs, self.ys, t)
        lu, lv = u - LOGO_AT[0], v - LOGO_AT[1]
        rgba = self._sample(self.asset, lu, lv)
        rgba[(lu < 0) | (lv < 0) | (lu > self.logo.width - 1) | (lv > self.logo.height - 1)] = 0
        ys, xs = np.nonzero(rgba[..., 3] > 0)
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        return SimpleNamespace(box=(x0, y0, x1, y1), image=Image.fromarray(np.clip(rgba[y0:y1, x0:x1], 0, 255).astype(np.uint8), "RGBA"))

    def mesh(self, t: float, cells: int = 8) -> np.ndarray:
        us = np.linspace(LOGO_AT[0], LOGO_AT[0] + self.logo.width, cells + 1)
        vs = np.linspace(LOGO_AT[1], LOGO_AT[1] + self.logo.height, cells + 1)
        return np.array([[self.forward(u, v, t) for u in us] for v in vs])

    def stamped(self, t: float) -> tuple[SimpleNamespace, np.ndarray]:
        """The old way: the asset at its start-frame size and shape, moved with the cloth's centre only."""
        first = self.mesh(0.0)
        corners = np.array([first[0, 0], first[0, -1], first[-1, -1], first[-1, 0]])
        centre = (LOGO_AT[0] + self.logo.width / 2, LOGO_AT[1] + self.logo.height / 2)
        quad = corners + np.subtract(self.forward(*centre, t), self.forward(*centre, 0.0))
        size = (round(quad[1, 0] - quad[0, 0]), round(quad[3, 1] - quad[0, 1]))
        x0, y0 = np.floor(quad.min(axis=0)).astype(int)
        picture = self.logo.resize(size, Image.Resampling.BILINEAR)
        return SimpleNamespace(box=(int(x0), int(y0), int(x0) + size[0], int(y0) + size[1]), image=picture), logo_integration.quad_grid(quad)


def _pasted(frame: np.ndarray, warped: SimpleNamespace) -> np.ndarray:
    """*warped* pasted over *frame* as the composited path drew it before: its colour as is, straight alpha."""
    x0, y0, x1, y1 = warped.box
    layer = np.asarray(warped.image, dtype=np.float32)
    alpha = layer[..., 3:4] / 255.0
    out = frame.copy()
    out[y0:y1, x0:x1] = np.clip(layer[..., :3] * alpha + frame[y0:y1, x0:x1] * (1 - alpha), 0, 255).astype(np.uint8)
    return out


def _measure(surface: Surface, way: str) -> logo_integration.Integration:
    probe = logo_integration.Probe("the brand logo", fps=FPS, size=(W, H))
    for index in range(SECONDS * FPS):
        if not probe.wants(index):
            continue
        t = index / FPS
        source = surface.frame(t)
        if way == "stamped":
            warped, vertices = surface.stamped(t)
            drawn = _pasted(source, warped)
        else:
            warped, vertices = surface.warped(t), surface.mesh(t)
            drawn = logo_integration.blend(source, warped) if way == "integrated" else _pasted(source, warped)
        probe.add(index, source, drawn, warped, vertices)
    return probe.measure()


@pytest.fixture(scope="module")
def quilted() -> Surface:
    return Surface()


def test_a_logo_stamped_flat_on_a_moving_quilted_surface_fails_naming_the_frame(quilted):
    result = _measure(quilted, "stamped")
    assert not result.passed and result.score < logo_integration.MIN_SCORE
    # Every measure read, and the one failing names its frame and its time in the shot.
    assert set(result.measures) == set(logo_integration.MEASURES)
    assert result.failed == "shading" and result.measures["size"]["score"] < logo_integration.MIN_SCORE
    assert result.frame is not None and result.frame % logo_integration.SAMPLE_EVERY == 0
    assert result.seconds == pytest.approx(result.frame / FPS) and result.frame_jpeg[:2] == b"\xff\xd8"
    reason = result.reason()
    assert reason.startswith(f"logo not integrated at t={result.seconds:.2f} s (frame {result.frame}) of the shot: the brand logo is lit flat")
    assert 'ingredients_quality: "flat"' in reason and "takes the logo as a picture" in reason


def test_the_same_logo_warped_onto_the_surface_and_drawn_into_its_light_passes(quilted):
    result = _measure(quilted, "integrated")
    assert result.passed and result.score >= logo_integration.MIN_SCORE, result.measures
    assert set(result.measures) == set(logo_integration.MEASURES)
    assert all(measure["score"] >= logo_integration.MIN_SCORE for measure in result.measures.values())
    assert result.frame is None and result.frame_jpeg is None


def test_warped_but_pasted_unlit_still_reads_flat(quilted):
    """Following the surface is not enough: a logo that takes none of its light is a sticker that moves well."""
    result = _measure(quilted, "warped")
    assert not result.passed and result.failed == "shading"
    assert result.measures["follow"]["score"] >= logo_integration.MIN_SCORE


def test_a_logo_moving_rigidly_over_a_surface_that_bends_fails_on_its_follow():
    bending = Surface(bend=40.0, grow=0.0, slide=0.0)
    probe = logo_integration.Probe("the brand logo", fps=FPS, size=(W, H))
    for index in range(0, 4 * FPS, logo_integration.SAMPLE_EVERY):
        t = index / FPS
        source = bending.frame(t)
        warped, vertices = bending.warped(0.0), bending.mesh(0.0)
        probe.add(index, source, logo_integration.blend(source, warped), warped, vertices)
    result = probe.measure()
    assert not result.passed and result.measures["follow"]["score"] < logo_integration.MIN_SCORE


def test_a_flat_logo_on_a_smooth_surface_lit_flat_is_no_defect():
    plain = np.full((H, W, 3), (40, 60, 120), dtype=np.uint8)
    mark = _logo()
    probe = logo_integration.Probe("the brand logo", fps=FPS, size=(W, H))
    warped = SimpleNamespace(box=(200, 120, 200 + mark.width, 120 + mark.height), image=mark)
    vertices = logo_integration.quad_grid(np.array([[200, 120], [420, 120], [420, 210], [200, 210]], dtype=float))
    for index in range(0, 2 * FPS, logo_integration.SAMPLE_EVERY):
        probe.add(index, plain, _pasted(plain, warped), warped, vertices)
    result = probe.measure()
    assert result.passed and "shading" not in result.measures


def test_the_take_keeps_its_integration_with_its_frame_and_never_a_status_alone(monkeypatch, quilted):
    stored: list[dict] = []

    async def store_bytes(data, **kwargs):
        stored.append({"data": data, **kwargs})
        return SimpleNamespace(id=uuid.UUID(int=6064))

    monkeypatch.setattr(runners.assets, "store_bytes", store_bytes)
    result = _measure(quilted, "stamped")
    check = {
        "status": logo_integration.NOT_INTEGRATED,
        "frames_sampled": 32,
        "presence": {"frames": 96, "drawn_frames": 96, "logos": 1},
        "similarity": {"min_ssim": 0.91, "places_compared": 96, "threshold": logos.MIN_SSIM},
        "integration": result.record(),
    }
    params: dict = {frame_qc.QC_KEY: {}}
    composited = logos.Composited(96, ["the brand logo"], [], check, None, result)
    asyncio.run(runners._record_logo_check(composited, params, base="shot", user_id=uuid.uuid4()))
    record = frame_qc.logo_check(params[frame_qc.QC_KEY])
    assert {"presence", "similarity", "integration"} <= set(record)
    integration = record["integration"]
    assert integration["passed"] is False and integration["failed"] == "shading" and integration["frame"] == result.frame
    assert {"shading", "follow", "size", "score"} <= set(integration)
    assert integration["frame_file_id"] == str(uuid.UUID(int=6064)) and stored[0]["data"] == result.frame_jpeg
    # The frame is a file of the take, deleted with it; the answer says the score.
    assert str(uuid.UUID(int=6064)) in frame_qc.frame_file_ids(params)
    assert frame_qc.integration_score(params[frame_qc.QC_KEY]) == pytest.approx(result.score)


def _document() -> dict:
    return {
        "scenes": [
            {"id": "intro", "type": "title", "start": 0.0, "end": 3.0},
            {"id": "crew", "type": "clip", "start": 3.0, "end": 11.0, "time_map": [[0.0, 0.0], [8.0, 8.0]]},
        ]
    }


def test_a_scene_preview_and_a_render_list_the_finding_in_the_scenes_time(quilted):
    result = _measure(quilted, "stamped")
    gate = frame_qc.logo_gate({frame_qc.LOGO_CHECK: {"status": "not_integrated", "integration": {**result.record(), "frame_file_id": "f9"}}})
    shots = {"crew": "clip-a"}
    found = logos.integration_findings(_document(), shots, {"clip-a": gate})
    assert len(found) == 1
    finding, frame_file = found[0]
    assert finding.kind == render_checks.LOGO_NOT_INTEGRATED and finding.severity == render_checks.FAIL
    assert finding.scene_id == "crew" and finding.at == pytest.approx(3.0 + result.seconds) and frame_file == "f9"
    assert finding.message.startswith(f"logo not integrated at t={render_checks.clock(3.0 + result.seconds)}: Scene crew")
    assert f"frame {result.frame} of the shot" in finding.message and 'ingredients_quality: "flat"' in finding.message
    # A scene preview of the shot: in the MP4's own time.
    preview = logos.integration_findings(_document(), shots, {"clip-a": gate}, offset=3.0, span=(3.0, 8.0))
    assert preview[0][0].at == pytest.approx(result.seconds)
    # Like an audio failure: the next render is refused; the renders panel labels it.
    checks = render_checks.Checks(findings=[finding]).as_dict()
    assert render_checks.LOGO_NOT_INTEGRATED in render_checks.PICTURE_KINDS
    assert render_checks.open_findings(checks, same_quality=False) and not render_checks.scene_passed(checks, "crew")
    # Accepted flat: recorded, never a finding.
    waived = frame_qc.logo_gate({frame_qc.LOGO_CHECK: {"status": "passed", "integration": result.record(waived=True)}})
    assert logos.integration_findings(_document(), shots, {"clip-a": waived}) == []


def test_the_tracked_paths_integration_is_read_by_the_render_too(quilted):
    result = _measure(quilted, "stamped")
    qc = {frame_qc.LOGO_TRACKED: {"status": "drawn", "integration": result.record()}}
    gate = frame_qc.logo_gate(qc)
    assert gate["integration"]["failed"] == "shading" and frame_qc.integration_score(qc) == pytest.approx(result.score)
    assert len(logos.integration_findings(_document(), {"crew": "clip-a"}, {"clip-a": gate})) == 1


def test_the_save_quality_check_refuses_it_and_flat_accepts_it_as_advice(quilted):
    record = _measure(quilted, "stamped").record()
    shot = quality.ShotFacts(name="crew", status="ready", logo_integration=record, ingredients_path="composited")
    severity, words = quality._logo_integration_words(shot)
    assert severity == "dense" and severity in render_checks.BLOCKING_SEVERITIES
    assert "as a flat sticker (ingredients_path: composited)" in words and "a render refuses it" in words
    flat = quality.ShotFacts(name="crew", status="ready", logo_integration={**record, "waived": "flat"})
    assert quality._logo_integration_words(flat)[0] == "advisory"
    assert quality._logo_integration_words(quality.ShotFacts(name="crew", status="ready")) is None


def test_ingredients_quality_is_integrated_or_flat():
    assert clips.ingredients_quality_asked("flat") == {"ingredients_quality": "flat"}
    assert clips.ingredients_quality_asked(" Flat ") == {"ingredients_quality": "flat"}
    assert clips.ingredients_quality_asked("integrated") == {} and clips.ingredients_quality_asked(None) == {}
    with pytest.raises(clips.ClipError, match="ingredients_quality"):
        clips.ingredients_quality_asked("sticker")
    assert "ingredients_quality" in clips.RETRY_FIELDS
    assert logo_integration.flat_accepted({"ingredients_quality": "flat"}) and not logo_integration.flat_accepted({})
