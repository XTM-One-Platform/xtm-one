"""Cross-scene consistency of a recurring prop or character. Synthetic pictures and specs only.

A video keeps ``references`` (a crop of the prop from the scene that got it
right); a clip scene names those its AI shot must match (``consistency``):

* F60: a character wearing a teal-piped prop against the same character with
  a navy collar and light-blue piping is a ``reference_drift``;
* F61: the same prop under another light is no drift;
* F62: a shot naming no reference is never read and gets no finding;
* F63: a spec naming a reference the video has none of is refused, naming it;
* F64: real footage (``fixtures/video_studio``: two frames of the Normandy film's booth scenes, the rug seen from
  the front and from behind, the rug's crop from another shot as the reference) is no drift;
* F65: the same frame with the rug's teal piping recoloured light blue is a drift (colour);
* F66: a frame without the prop (a product screen) is an advisory ``reference not seen``, never a dense finding
  and never a failed take.

And ``generate_video_clip``: the references go as the model's reference
images where it takes them beside the start frame, else they are named in the
prompt; the answer says which.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageEnhance

from app.services.video_studio import clips, reference_drift, timeline
from app.services.video_studio.quality import ContentFacts, ShotFacts, content_findings
from app.services.video_studio.spec import (
    VideoSpecError,
    authored_spec,
    consistency_of,
    parse_video_spec,
    references_of,
    validate_video_spec,
)

pytestmark = pytest.mark.usefixtures("no_default_video_template")

FOOTAGE = Path(__file__).parent / "fixtures" / "video_studio"

SHOT, OTHER = "00000000-0000-0000-0000-00000000c001", "00000000-0000-0000-0000-00000000c002"
RUG = "00000000-0000-0000-0000-00000000f001"
FRAME = "00000000-0000-0000-0000-00000000f101"
START = "00000000-0000-0000-0000-00000000f002"
VIDEO = "00000000-0000-0000-0000-00000000e001"
NAVY, TEAL, LIGHT_BLUE, WHITE = (24, 38, 84), (0, 150, 150), (120, 190, 240), (245, 245, 245)
BROWN, GRASS, SKY = (120, 80, 40), (70, 130, 50), (170, 200, 230)


def _prop(piping=TEAL, collar: bool = False, size=(160, 100)) -> Image.Image:
    """The branded rug: navy quilted body, piping along its edges, a white emblem; a collar when asked."""
    rug = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(rug)
    w, h = size
    draw.rectangle([0, 0, w - 1, h - 1], outline=piping, width=max(3, w // 24))
    for x in range(0, w, w // 6):
        draw.line([(x, 0), (x, h)], fill=(34, 50, 104), width=1)
    for y in range(0, h, h // 4):
        draw.line([(0, y), (w, y)], fill=(34, 50, 104), width=1)
    draw.ellipse([w * 0.38, h * 0.3, w * 0.62, h * 0.7], fill=WHITE)
    draw.rectangle([w * 0.3, h * 0.78, w * 0.7, h * 0.88], fill=WHITE)
    if collar:
        draw.rectangle([0, 0, w - 1, h * 0.22], fill=NAVY)
        draw.rectangle([0, h * 0.16, w - 1, h * 0.22], fill=piping)
    return rug


def _scene(prop: Image.Image, *, at=(260, 130), light: float = 1.0, size=(640, 360)) -> Image.Image:
    """A frame of the shot: the character (a brown body) wearing *prop* on a meadow under a sky."""
    frame = Image.new("RGB", size, SKY)
    draw = ImageDraw.Draw(frame)
    draw.rectangle([0, size[1] * 0.55, size[0], size[1]], fill=GRASS)
    x, y = at
    draw.ellipse([x - 60, y - 40, x + prop.width + 60, y + prop.height + 50], fill=BROWN)
    frame.paste(prop, (x, y))
    if light != 1.0:
        frame = ImageEnhance.Brightness(frame).enhance(light)
    return frame


def _frames(prop: Image.Image, **kw) -> dict[str, Image.Image]:
    return {
        "first_frame_file_id": _scene(prop, at=(250, 128), **kw),
        "mid_frame_file_id": _scene(prop, at=(262, 132), **kw),
        "last_frame_file_id": _scene(prop, at=(274, 126), **kw),
    }


REFERENCE = {
    "id": "rug",
    "file_id": RUG,
    "label": "the branded rug",
    "kind": "prop",
    "description": "navy quilted rug, teal piping",
}


# ── F60 / F61: the measure ───────────────────────────────────────────────


def test_f60_a_collar_and_light_blue_piping_drift_from_the_teal_piped_reference():
    reference = _prop()
    drifted = _prop(piping=LIGHT_BLUE, collar=True)
    drifts = reference_drift.drifts_from_frames([REFERENCE], _frames(drifted), {"rug": reference})
    (drift,) = drifts
    assert drift.reference["id"] == "rug" and drift.measures
    worst = drift.worst
    assert worst.delta_e > reference_drift.MAX_DELTA_E or worst.similarity < reference_drift.MIN_SIMILARITY
    assert drift.words().startswith("reference drift: the branded rug differs from the video's reference `rug`")
    assert drift.crop_words().startswith("crop: the ") and " at x " in drift.crop_words()
    # The finding carries the crop: the frame it was read on and the box, 0-1 across and down it.
    data = drift.as_dict()
    assert data["reference"]["id"] == "rug" and all(0.0 <= v <= 1.0 for v in data["measures"][0]["box"])
    assert "generate it again" in reference_drift.take_reason(drifts)


def test_f61_the_same_prop_under_another_light_is_no_drift():
    reference = _prop()
    same = _prop()
    assert reference_drift.drifts_from_frames([REFERENCE], _frames(same), {"rug": reference}) == []
    relit = reference_drift.drifts_from_frames([REFERENCE], _frames(same, light=0.7), {"rug": reference})
    assert relit == [], [m.as_dict() for d in relit for m in d.measures]
    # The crop is found where the prop stands, not read off the whole frame, and a brighter light holds too.
    measure = reference_drift.compare(reference, _scene(same, light=1.3))
    assert measure.located and measure.similarity >= reference_drift.MIN_SIMILARITY
    assert measure.delta_e <= reference_drift.MAX_DELTA_E
    x0, y0, x1, y1 = measure.box
    assert 0.3 < x0 < 0.45 and 0.55 < x1 < 0.75 and 0.25 < y0 < 0.45 and 0.55 < y1 < 0.75, measure.box


# ── F64 / F65 / F66: real footage ────────────────────────────────────────


def _footage(name: str) -> Image.Image:
    return Image.open(FOOTAGE / name)


def test_f64_the_rug_on_real_footage_seen_from_another_angle_is_no_drift():
    """The rug's crop (one shot, from the flank) against two shots of the booth: from the front, and four cows from
    behind at a tenth of the frame. The old locator never found it and the fallback read the whole frame."""
    reference = _footage("rug_reference.png")
    frames = {
        "first_frame_file_id": _footage("booth_welcome.jpg"),
        "mid_frame_file_id": _footage("booth_arrival.jpg"),
        "last_frame_file_id": _footage("booth_welcome.jpg"),
    }
    drifts = reference_drift.drifts_from_frames([REFERENCE], frames, {"rug": reference})
    assert drifts == [], [m.as_dict() for d in drifts for m in d.measures]
    measure = reference_drift.compare(reference, frames["first_frame_file_id"])
    assert measure.located and measure.match >= reference_drift.MIN_LOCATE
    assert measure.delta_e <= reference_drift.MAX_DELTA_E and measure.similarity >= reference_drift.MIN_SIMILARITY
    # Found on the cow, not on the booth's wall logo or the crowd: the box covers the rug's place in the frame.
    x0, y0, x1, y1 = measure.box
    assert 0.4 < x0 < 0.6 and 0.7 < x1 < 0.9 and 0.25 < y0 < 0.45 and 0.6 < y1 < 0.8, measure.box


def test_f65_the_piping_recoloured_light_blue_on_real_footage_is_a_colour_drift():
    reference = _footage("rug_reference.png")
    frames = {"first_frame_file_id": _footage("booth_welcome_light_blue.jpg")}
    (drift,) = reference_drift.drifts_from_frames([REFERENCE], frames, {"rug": reference})
    assert drift.found and drift.measures[0].located
    assert drift.measures[0].delta_e > reference_drift.MAX_DELTA_E
    assert "colour dE" in drift.words() and drift.words().startswith("reference drift: the branded rug differs")
    assert reference_drift.failures([drift]) == [drift]


def test_f66_a_frame_without_the_prop_is_an_advisory_not_a_drift():
    reference = _footage("rug_reference.png")
    frames = {"first_frame_file_id": _footage("screen_insert.jpg"), "last_frame_file_id": _footage("screen_insert.jpg")}
    (said,) = reference_drift.drifts_from_frames([REFERENCE], frames, {"rug": reference})
    assert not said.found and not any(m.located for m in said.measures) and not any(m.drifted for m in said.measures)
    assert said.words().startswith("reference not seen: the branded rug is not seen in the shot")
    assert "match" in said.words() and said.crop_words().startswith("crop: the ")
    # Never a failed take: the worker keeps the take and notes it.
    assert reference_drift.failures([said]) == []
    # A reference seen in one frame of three is judged on that frame alone.
    seen = reference_drift.Measure("middle", None, 0.9, 3.0, (0.4, 0.3, 0.8, 0.7), 0.8)
    unseen = reference_drift.Measure("first", None, 0.9, 30.0, (0.0, 0.0, 0.5, 0.6), 0.6)
    assert reference_drift.judge(REFERENCE, [unseen, seen]) is None
    assert reference_drift.judge(REFERENCE, [unseen]).found is False
    # The measure reads back from the cache with its match.
    assert reference_drift.Measure.from_dict(seen.as_dict()) == seen


def test_a_reference_the_frames_have_no_picture_of_is_skipped():
    assert reference_drift.drifts_from_frames([REFERENCE], _frames(_prop()), {}) == []


# ── The spec: references on the video, consistency on a clip scene ──────


def _raw(consistency: list[str] | None = None, references: list[dict] | None = None, second: bool = False) -> dict:
    scene: dict = {"id": "booth", "type": "clip", "clip_id": SHOT}
    if consistency is not None:
        scene["consistency"] = consistency
    scenes = [scene]
    if second:
        scenes.append({"id": "street", "type": "clip", "clip_id": OTHER})
    raw: dict = {"title": "Synthetic", "freestyle": True, "scenes": scenes}
    if references is not None:
        raw["references"] = references
    return raw


def test_f63_a_shot_naming_an_unknown_reference_is_refused_naming_it():
    with pytest.raises(VideoSpecError) as refused:
        validate_video_spec(_raw(["rug", "hat"], [REFERENCE]))
    said = str(refused.value)
    assert "'hat'" in said and "the video's references: rug" in said
    with pytest.raises(VideoSpecError, match="used twice"):
        validate_video_spec(_raw(["rug"], [REFERENCE, {**REFERENCE, "label": "again"}]))
    with pytest.raises(VideoSpecError, match="each reference once"):
        validate_video_spec(_raw(["rug", "rug"], [REFERENCE]))
    with pytest.raises(VideoSpecError):
        validate_video_spec(_raw(["rug"], [{**REFERENCE, "kind": "vehicle"}]))


def test_references_and_consistency_are_kept_and_read_back():
    spec = validate_video_spec(_raw(["rug"], [REFERENCE], second=True))
    assert references_of(spec) == {"rug": {**REFERENCE}}
    assert consistency_of(spec) == {SHOT: [REFERENCE]}
    parsed = parse_video_spec(spec)
    assert parsed.references[0].label == "the branded rug" and parsed.scenes[0].consistency == ["rug"]
    # A video without references validates as before (backward compatible), and a shot naming none is held to none.
    plain = validate_video_spec(_raw())
    assert references_of(plain) == {} and consistency_of(plain) == {}


# ── The save-time quality check ──────────────────────────────────────────


def _findings(raw: dict, facts: ContentFacts) -> list:
    spec = parse_video_spec(validate_video_spec(raw))
    shots = {
        scene.id: timeline.ClipMedia(duration=4.0, width=1920, height=1080, fps=24.0, frame_count=96)
        for scene in spec.scenes
        if scene.type == "clip"
    }
    document = timeline.estimate_plan(spec, {}, shots).document
    return content_findings(spec, document, facts, authored_spec(raw))


def _facts(**changes) -> ContentFacts:
    facts = ContentFacts(
        shots={
            SHOT: ShotFacts("Booth", "ready", width=1920, height=1080, camera="unspecified", motion_ssim=0.9),
            OTHER: ShotFacts("Street", "ready", width=1920, height=1080, camera="unspecified", motion_ssim=0.9),
        },
        output=(1920, 1080),
    )
    for key, value in changes.items():
        setattr(facts, key, value)
    return facts


def test_a_shot_that_drifted_from_a_reference_is_a_dense_finding_with_the_crop():
    drift = reference_drift.judge(
        REFERENCE, [reference_drift.Measure("first", FRAME, 0.62, 19.0, (0.3, 0.4, 0.5, 0.7), 0.9)]
    )
    found = _findings(_raw(["rug"], [REFERENCE], second=True), _facts(reference_drifts={SHOT: [drift]}))
    (finding,) = [f for f in found if f.kind == "reference_drift"]
    assert finding.scene_id == "booth" and finding.severity == "dense" and finding.clip_id == SHOT
    assert finding.message.startswith("reference drift: the branded rug differs from the video's reference `rug`")
    assert f"crop: the first frame (file `{FRAME}`) at x 0.30-0.50, y 0.40-0.70" in finding.message
    assert finding.data["reference"]["id"] == "rug" and finding.data["measures"][0]["box"] == [0.3, 0.4, 0.5, 0.7]
    assert finding.data["found"] is True


def test_f66_a_reference_not_seen_in_the_shot_is_an_advisory_finding():
    unseen = reference_drift.judge(
        REFERENCE, [reference_drift.Measure("first", FRAME, 0.7, 30.0, (0.0, 0.0, 0.5, 0.6), 0.66)]
    )
    found = _findings(_raw(["rug"], [REFERENCE], second=True), _facts(reference_drifts={SHOT: [unseen]}))
    (finding,) = [f for f in found if f.kind == "reference_drift"]
    assert finding.severity == "advisory" and finding.clip_id == SHOT
    assert finding.message.startswith("reference not seen: the branded rug is not seen in the shot")
    assert "check the shot by eye" in finding.message and finding.data["found"] is False


def test_f62_a_shot_without_consistency_gets_no_finding_even_when_a_measure_exists():
    """The street shot names no reference: whatever is measured about it is never a finding of its scene."""
    drift = reference_drift.judge(
        REFERENCE, [reference_drift.Measure("last", None, 0.2, 40.0, (0.0, 0.0, 1.0, 1.0), 0.9)]
    )
    found = _findings(_raw(["rug"], [REFERENCE], second=True), _facts(reference_drifts={OTHER: [drift]}))
    assert not [f for f in found if f.kind == "reference_drift"]
    assert not [f for f in _findings(_raw(), _facts()) if f.kind == "reference_drift"]


# ── generate_video_clip: the references reach the model, and the answer says how ──


class _Veo:
    """Veo 3.1: three reference images, never beside a start frame."""

    def max_reference_images(self, model):
        return 3

    def reference_images_with_start_frame(self, model):
        return False


class _Omni(_Veo):
    def reference_images_with_start_frame(self, model):
        return True


@pytest.fixture
def video(monkeypatch):
    from app.services.video_studio import projects

    async def load(_db, video_id, _user):
        if str(video_id) != VIDEO:
            return None
        return SimpleNamespace(spec={"title": "v", "scenes": [], "references": [REFERENCE]})

    monkeypatch.setattr(projects, "load_visible_project", load)


def _held(ids, *, provider, start_frame: bool, video_id=VIDEO):
    return asyncio.run(
        clips.resolve_consistency(
            None, None, ids, video_id=video_id, provider=provider, model="m", start_frame=start_frame, reference_images=0
        )
    )


def test_the_references_go_as_reference_images_where_the_model_takes_them_beside_a_start_frame(video):
    held = _held(["rug"], provider=_Omni(), start_frame=True)
    assert held["path"] == clips.PATH_NATIVE and held["references"][0]["file_id"] == RUG
    params: dict = {"reference_file_id": START}
    clips.keep_consistency(params, held)
    assert params[clips.REFERENCE_IMAGES] == [RUG]
    assert clips.consistency_prompt("A cow walks in.", params) == "A cow walks in."
    assert "sent as the model's reference images" in clips.consistency_note(params)


def test_the_references_are_named_in_the_prompt_where_the_model_takes_none_beside_a_start_frame(video):
    held = _held(["rug"], provider=_Veo(), start_frame=True)
    assert held["path"] == clips.PATH_PROMPT
    params: dict = {"reference_file_id": START}
    clips.keep_consistency(params, held)
    assert clips.REFERENCE_IMAGES not in params
    assert clips.consistency_prompt("A cow walks in.", params) == (
        "A cow walks in. Keep the branded rug exactly as in the start picture: navy quilted rug, teal piping."
    )
    assert "named in the prompt" in clips.consistency_note(params)
    # Without a start frame the description alone holds it.
    bare: dict = {}
    clips.keep_consistency(bare, held)
    assert clips.consistency_prompt("A cow walks in.", bare) == (
        "A cow walks in. Keep the branded rug: navy quilted rug, teal piping."
    )


def test_an_unknown_reference_or_no_video_is_refused_before_anything_is_paid(video):
    with pytest.raises(clips.ClipError, match="names no reference of the video: 'hat'"):
        _held(["hat"], provider=_Omni(), start_frame=False)
    with pytest.raises(clips.ClipError, match="name the video"):
        _held(["rug"], provider=_Omni(), start_frame=False, video_id=None)
    assert _held([], provider=_Omni(), start_frame=False) is None
    with pytest.raises(clips.ClipError, match="at most"):
        clips._consistency_ids(["a", "b", "c", "d"])
    # Dropped: the params forget them.
    params = {clips.CONSISTENCY: {"path": "native"}, clips.REFERENCE_IMAGES: [RUG]}
    clips.keep_consistency(params, None)
    assert clips.CONSISTENCY not in params


def test_the_generate_video_clip_tool_offers_consistency():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    tools, _executor = build_video_studio_tools(None, str(uuid.uuid4()), None)
    (tool,) = [t for t in tools if t.name == "generate_video_clip"]
    properties = tool.input_schema["properties"]
    assert properties["consistency"]["items"] == {"type": "string"}
    assert "reference drift" in properties["consistency"]["description"]
