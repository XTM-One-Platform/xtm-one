"""The brand's mark once on a drawn frame: ``duplicate_logo`` and a scene's ``watermark: false``.

* Synthetic footage with the template's logo painted on a wall and the watermark on gives ``duplicate_logo`` with
  both boxes and the fix "watermark off on that scene"; with ``watermark: false`` on that scene, no finding.
* The finding is a picture failure: it refuses the next render.
* A scene's ``watermark: false`` is planned on the scene only when false.
* The sweep names the new gates on its red tiles.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.services.video_studio import insert_checks, insert_sweep, logos, render_checks
from app.services.video_studio.spec import VideoSpec


def _logo() -> Image.Image:
    """A small two-colour brand mark (RGBA): a disc and a bar, transparent around them."""
    mark = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    pen = ImageDraw.Draw(mark)
    pen.ellipse((4, 4, 56, 56), fill=(230, 60, 40, 255))
    pen.rectangle((64, 14, 116, 46), fill=(30, 90, 220, 255))
    pen.rectangle((76, 24, 104, 36), fill=(250, 250, 250, 255))
    return mark


def _wall(*, painted: bool, watermark: bool) -> Image.Image:
    """A 640x360 frame of a textured wall, the logo painted on it, the corner watermark drawn over it."""
    rng = np.random.default_rng(7)
    base = np.clip(rng.normal(120, 18, (360, 640, 3)), 0, 255).astype(np.uint8)
    frame = Image.fromarray(base, "RGB")
    logo = _logo()
    if painted:
        frame.paste(logo.resize((200, 100)), (180, 120), logo.resize((200, 100)))
    if watermark:
        frame.paste(logo.resize((80, 40)), (540, 20), logo.resize((80, 40)))
    return frame


def _document(scene_watermark: bool | None) -> tuple[dict, dict]:
    scene: dict = {"id": "s1", "type": "clip", "start": 0.0, "end": 4.0}
    if scene_watermark is False:
        scene["watermark"] = False
    return {"watermark": "bottom-right", "scenes": [scene], "duration": 4.0}, scene


def test_a_logo_painted_on_the_wall_under_the_watermark_is_a_duplicate_logo():
    document, scene = _document(None)
    frame = _wall(painted=True, watermark=logos.scene_watermarked(document, scene))
    places = logos.brand_marks_on(frame, {"logo": _logo()})
    assert len(places) >= 2
    finding = logos.duplicate_logo_finding(scene, places, at=1.5, watermarked=True)
    assert finding is not None and finding.kind == render_checks.DUPLICATE_LOGO
    assert finding.severity == render_checks.FAIL
    assert len(finding.data["boxes"]) == 2 and finding.data["watermark"] is True
    assert "watermark: false" in (finding.fix or "")
    # The frame it shows has both places outlined.
    assert logos.boxed_frame_jpeg(frame, [tuple(b) for b in finding.data["boxes"]])[:2] == b"\xff\xd8"


def test_watermark_false_on_that_scene_leaves_one_logo_and_no_finding():
    document, scene = _document(False)
    assert logos.scene_watermarked(document, scene) is False
    frame = _wall(painted=True, watermark=logos.scene_watermarked(document, scene))
    places = logos.brand_marks_on(frame, {"logo": _logo()})
    assert len(places) == 1
    assert logos.duplicate_logo_finding(scene, places, at=1.5, watermarked=False) is None


def test_two_logos_in_the_shot_without_a_watermark_name_the_shot():
    scene = {"id": "s2", "type": "clip"}
    places = [("logo", (0.1, 0.1, 0.3, 0.2), 0.97), ("logo", (0.6, 0.6, 0.8, 0.7), 0.95)]
    finding = logos.duplicate_logo_finding(scene, places, at=0.5, watermarked=False, clip_id="c1")
    assert finding is not None and "clip_id" in (finding.fix or "") and finding.data["clip_id"] == "c1"


def test_a_card_carrying_the_brand_has_no_watermark_over_it():
    document = {"watermark": "bottom-right"}
    assert logos.scene_watermarked(document, {"type": "title"}) is False
    assert logos.scene_watermarked(document, {"type": "statement", "show_logo": True}) is False
    assert logos.scene_watermarked(document, {"type": "clip"}) is True
    assert logos.scene_watermarked({"watermark": "none"}, {"type": "clip"}) is False


def test_duplicate_logo_refuses_the_next_render():
    assert render_checks.DUPLICATE_LOGO in render_checks.PICTURE_KINDS
    checks = {"findings": [{"kind": render_checks.DUPLICATE_LOGO, "severity": render_checks.FAIL, "at": 1.0, "scene_id": "s1", "message": "m"}]}
    assert render_checks.open_findings(checks, same_quality=False)


def test_a_scene_turns_the_watermark_off_backward_compatibly():
    fields = VideoSpec.model_json_schema()
    assert "watermark" in str(fields)
    from app.services.video_studio import spec as spec_module

    assert spec_module._SceneBase.model_fields["watermark"].default is None


def test_the_sweep_names_the_new_gates_in_red():
    assert insert_sweep.gate_label("matte_halo") == "insert_matte_halo"
    assert insert_sweep.gate_label("matte_lag") == "insert_matte_lag"
    assert insert_sweep.gate_label("occluded") == "insert_occluded"
    assert "duplicate_logo" in insert_sweep.SCENE_FINDINGS
    failing = insert_checks.failing_frames({0: {"foreground_share": 0.5}, 1: {"foreground_share": 0.1}}, legible=True)
    assert failing == {0: ["occluded"]}
    assert insert_checks.failing_frames({0: {"foreground_share": 0.5}}) == {}
