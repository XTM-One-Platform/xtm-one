"""An animated still without any model: a real screenshot given motion by effects the compositor draws.

Parallax layers lift panels of the image off it, a light sweep crosses it, a
pulse glows on one region; all drawn on the real image as a pure function of
time, so its text is never redrawn and the render and the preview agree.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.services.video_studio import compositor_page, timeline
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

IMAGE = "00000000-0000-0000-0000-00000000e001"
EFFECTS = [
    {"type": "parallax", "layers": [{"box": [0.1, 0.2, 0.3, 0.25], "depth": 0.8}]},
    {"type": "light_sweep", "at": 0.6, "duration": 1.6},
    {"type": "pulse", "box": [0.6, 0.1, 0.2, 0.2], "shape": "ellipse", "at": 0.5, "period": 1.6, "count": 1},
]


def _spec(scene: dict) -> dict:
    return {"title": "Animated still", "freestyle": True, "scenes": [scene]}


def _image(**fields) -> dict:
    return {"id": "still", "type": "image", "file_id": IMAGE, "duration": 6, **fields}


def test_an_image_scene_takes_effects_each_checked():
    spec = parse_video_spec(validate_video_spec(_spec(_image(effects=EFFECTS))))
    kinds = [effect.type for effect in spec.scenes[0].effects]
    assert kinds == ["parallax", "light_sweep", "pulse"]
    assert spec.scenes[0].effects[1].angle == 20.0 and spec.scenes[0].effects[2].strength == 0.6


@pytest.mark.parametrize(
    ("effect", "wording"),
    [
        ({"type": "pulse", "box": [0.9, 0.1, 0.3, 0.2]}, "inside it"),
        ({"type": "parallax", "layers": []}, "at least 1"),
        ({"type": "light_sweep", "duration": 20}, "less than or equal to 8"),
        ({"type": "glow"}, "parallax"),
    ],
    ids=["box-outside", "no-layer", "sweep-too-long", "unknown-type"],
)
def test_an_effect_out_of_its_bounds_is_refused_by_name(effect, wording):
    with pytest.raises(VideoSpecError) as caught:
        validate_video_spec(_spec(_image(effects=[effect])))
    assert wording in str(caught.value)


def test_an_image_moves_by_a_model_or_by_effects_not_both():
    with pytest.raises(VideoSpecError) as caught:
        validate_video_spec(_spec(_image(animate=True, effects=EFFECTS[:1])))
    assert "not both" in str(caught.value)


def test_the_planner_hands_the_effects_to_the_compositor():
    spec = parse_video_spec(validate_video_spec(_spec(_image(effects=EFFECTS))))
    media = timeline.PlanMedia(images={"still": timeline.ImageMedia(1600, 900)})
    scene = timeline.plan(spec, media, fps=30, theme={}).document["scenes"][0]
    assert scene["type"] == "image" and [e["type"] for e in scene["effects"]] == ["parallax", "light_sweep", "pulse"]
    plain = parse_video_spec(validate_video_spec(_spec(_image())))
    assert "effects" not in timeline.plan(plain, media, fps=30, theme={}).document["scenes"][0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_effect_is_drawn_when_it_plays_and_rests_otherwise(tmp_path):
    source = compositor_page.PAGE_JS
    start = source.index("const clamp = ")
    helpers = source[start : source.index("const el = ", start)]
    functions = source[source.index("function parallaxShift(") : source.index("function buildStill(")]
    harness = """
const el = (tag, cls, parent) => {
  const node = { tag, className: cls, style: {}, children: [] };
  if (parent) parent.children.push(node);
  return node;
};
function setImg(img, url) { img.src = url; }
const stage = el('div', 'fxstage');
const sc = { motion: 'zoom_in', effects: JSON.parse(process.argv[2]) };
const draw = buildEffects(stage, sc, 'https://studio.render/image/still', 1000, 600);
const at = (t) => {
  draw(t, Math.min(1, t / 6));
  return JSON.parse(JSON.stringify(stage.children));
};
console.log(JSON.stringify({ rest: at(0), mid: at(1.3), late: at(4) }));
"""
    script = tmp_path / "effects.js"
    script.write_text(helpers + functions + harness, encoding="utf-8")
    # What the planner hands the compositor: the effects as validated, every default filled.
    spec = parse_video_spec(validate_video_spec(_spec(_image(effects=EFFECTS))))
    planned = timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={}).document["scenes"][0]["effects"]
    result = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", str(script), json.dumps(planned)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    frames = json.loads(result.stdout)

    def part(frame: str, cls: str) -> dict:
        return next(node for node in frames[frame] if node["className"] == cls)

    # The lifted panel is a copy of the real image, cut to its box.
    lift = part("rest", "lift")
    assert lift["children"][0]["src"] == "https://studio.render/image/still"
    assert lift["style"]["left"] == "10%" and lift["children"][0]["style"]["width"].startswith("333.33")
    # At rest nothing shows; mid-scene the panel is lifted, the sweep crosses, the pulse is at its peak.
    assert part("rest", "socket")["style"]["opacity"] == "0.000"
    assert part("mid", "socket")["style"]["opacity"] == "0.350"
    assert part("mid", "lift")["style"]["transform"] != part("rest", "lift")["style"]["transform"]
    assert part("mid", "sweep")["children"][0]["style"]["opacity"] == "1"
    assert part("mid", "pulse round")["style"]["opacity"] == "0.600"
    # Once the sweep has crossed and the one pulse is over, both rest again.
    assert part("late", "sweep")["children"][0]["style"]["opacity"] == "0"
    assert part("late", "pulse round")["style"]["opacity"] == "0.000"
