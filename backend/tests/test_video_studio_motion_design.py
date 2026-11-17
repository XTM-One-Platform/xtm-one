"""Motion design: the brand's motion tokens, motion scenes, beat sync, motion blur and the templates' motion."""

from __future__ import annotations

import json
import uuid

import numpy as np
import pytest

from app.services.video_studio import beats, motion_design, render_preflight, templates, timeline
from app.services.video_studio.encoder import motion_blur_filter
from app.services.video_studio.spec import (
    MOTION_COMPOSITIONS,
    VideoSpecError,
    parse_video_spec,
    referenced_ids,
    validate_video_spec,
)
from app.services.video_studio.themes import build_theme

FILE = str(uuid.UUID(int=7))
COMPOSITION_TEXTS = {
    "title": "Uncover threats. *Take action.*",
    "subtitle": "The open-source XTM suite",
    "kicker": "Filigran",
    "figure": "6,000+",
    "label": "community members",
    "items": ["Open source", "Community led", "Enterprise ready"],
    "lines": ["Too much *noise*.", "Not enough *context*."],
}


def _spec(*scenes: dict, **top) -> object:
    return parse_video_spec(validate_video_spec({"title": "Motion", "scenes": list(scenes), **top}))


def _plan(spec, media: timeline.PlanMedia | None = None, *, logo: bool = True) -> timeline.TimelinePlan:
    theme = build_theme(mode="dark", background_style="aurora", has_logo=logo, motion=spec.theme.motion)
    return timeline.plan(spec, media or timeline.PlanMedia(), fps=30, theme=theme)


# ── The tokens ────────────────────────────────────────────────────────────


def test_precise_holds_the_numbers_the_page_drew_with_before_tokens():
    tokens = motion_design.resolve_motion(None)
    assert tokens["personality"] == "precise" and tokens["time"] == 1.0
    assert tokens["ease"] == {"enter": "out", "exit": "in", "move": "in_out", "emphasis": "out_back"}
    assert tokens["title"] == {
        "split": "words",
        "style": "rise",
        "duration": 0.7,
        "distance": 46,
        "blur": 14,
        "rotate": 2,
        "scale": 1.0,
        "stagger": 0.065,
    }
    assert tokens["block"] == {"duration": 0.7, "distance": 40, "blur": 12, "step": 0.55, "word": 0.07, "drift": 0.02}
    assert tokens["card"]["drift"] == 0.025 and tokens["card"]["logo"] == "rise" and tokens["card"]["accent"] == "none"
    assert tokens["overlay"]["duration"] == 0.38
    assert tokens["beat_sync"] == "off" and tokens["motion_blur"] == "off"


def test_a_video_without_motion_is_drawn_with_the_precise_tokens():
    theme = build_theme(mode="dark", background_style="aurora")
    assert theme["motion"] == motion_design.resolve_motion(None)


def test_speed_and_the_overrides_shape_a_personality():
    tokens = motion_design.resolve_motion({"personality": "cinematic", "speed": 2.0, "title": "words", "logo": "fade", "accent": "none"})
    base = motion_design.resolve_motion({"personality": "cinematic"})
    assert tokens["time"] == pytest.approx(base["time"] / 2)
    assert tokens["title"]["duration"] == pytest.approx(base["title"]["duration"] / 2)
    assert tokens["block"]["step"] == pytest.approx(base["block"]["step"] / 2)
    assert tokens["title"]["split"] == "words" and tokens["card"]["logo"] == "fade" and tokens["card"]["accent"] == "none"
    assert base["card"]["accent"] == "sweep" and base["title"]["style"] == "blur"


@pytest.mark.parametrize("personality", motion_design.PERSONALITIES)
def test_every_personality_resolves_every_token_the_page_reads(personality):
    tokens = motion_design.resolve_motion({"personality": personality})
    assert set(tokens["ease"]) == {"enter", "exit", "move", "emphasis"}
    assert {"split", "style", "duration", "distance", "blur", "rotate", "scale", "stagger"} <= set(tokens["title"])
    assert {"duration", "distance", "blur", "step", "word", "drift"} <= set(tokens["block"])
    assert {"logo", "kicker", "rule", "cta", "accent", "drift"} <= set(tokens["card"])


def test_the_page_falls_back_on_the_precise_tokens():
    from app.services.video_studio.compositor_page import PAGE_JS

    assert "__DEFAULT_MOTION__" not in PAGE_JS
    assert f"const DEFAULT_MOTION = {json.dumps(motion_design.resolve_motion(None))};" in PAGE_JS


# ── The spec ──────────────────────────────────────────────────────────────


def _problems(*scenes: dict) -> str:
    with pytest.raises(VideoSpecError) as caught:
        validate_video_spec({"title": "T", "scenes": list(scenes)})
    return str(caught.value)


def test_a_motion_scene_draws_a_composition_or_elements_and_a_composition_its_texts():
    assert "draws a composition" in _problems({"type": "motion"})
    assert "needs its title" in _problems({"type": "motion", "composition": "kinetic_title"})
    assert "needs its figure" in _problems({"type": "motion", "composition": "stat_counter"})
    assert "counts a number" in _problems({"type": "motion", "composition": "stat_counter", "figure": "many"})
    assert "two lines or more" in _problems({"type": "motion", "composition": "split_statement", "lines": ["One"]})
    assert _spec({"type": "motion", "composition": "logo_reveal"}).scenes[0].composition == "logo_reveal"


def test_an_element_takes_the_fields_of_its_kind_only():
    assert "text is not a field of a shape element" in _problems({"type": "motion", "elements": [{"kind": "shape", "text": "x"}]})
    assert "needs its text" in _problems({"type": "motion", "elements": [{"kind": "text"}]})
    assert "one picture" in _problems({"type": "motion", "elements": [{"kind": "image"}]})
    assert "needs its file_id" in _problems({"type": "motion", "elements": [{"kind": "lottie"}]})
    assert "8 design pixels" in _problems({"type": "motion", "elements": [{"kind": "text", "text": "x", "size": 4}]})
    assert "40 design pixels" in _problems({"type": "motion", "elements": [{"kind": "particles", "size": 60}]})
    assert "time order" in _problems({"type": "motion", "elements": [{"kind": "text", "text": "x", "keys": [{"t": 2}, {"t": 1}]}]})
    assert "no element listed before it" in _problems(
        {"type": "motion", "elements": [{"kind": "text", "text": "x", "below": "later"}, {"id": "later", "kind": "text", "text": "y"}]}
    )


def test_path_data_is_path_commands_and_numbers_only():
    assert "SVG path commands" in _problems({"type": "motion", "elements": [{"kind": "path", "d": "M0 0 L1 1'><script>"}]})
    assert "SVG path commands" in _problems({"type": "motion", "elements": [{"kind": "path", "d": "L 0 0"}]})
    scene = _spec({"type": "motion", "elements": [{"kind": "path", "d": "M0 0   L10 10 Z", "morph_to": "m 0 0 c 1 1 2 2 3 3"}]}).scenes[0]
    assert scene.elements[0].d == "M0 0 L10 10 Z"


def test_an_easing_is_a_name_a_bezier_or_a_spring():
    scene = _spec(
        {
            "type": "motion",
            "elements": [
                {"kind": "text", "text": "x", "enter": {"preset": "pop", "ease": "Spring(170, 14)"}},
                {"kind": "text", "text": "y", "keys": [{"t": 0, "x": 0.1}, {"t": 1, "x": 0.9, "ease": "cubic-bezier(0.2, 1, 0.3, 1)"}]},
            ],
        }
    ).scenes[0]
    assert scene.elements[0].enter.ease == "spring(170, 14)"
    assert scene.elements[1].keys[1].ease == "cubic-bezier(0.2, 1, 0.3, 1)"
    assert "an easing is one of" in _problems({"type": "motion", "elements": [{"kind": "text", "text": "x", "enter": {"ease": "bounce"}}]})
    assert "x1 and x2 within 0-1" in _problems(
        {"type": "motion", "elements": [{"kind": "text", "text": "x", "enter": {"ease": "cubic-bezier(2, 0, 0, 1)"}}]}
    )


def test_a_motion_scene_draws_a_bounded_number_of_particles():
    many = [{"kind": "particles", "number": 400} for _ in range(4)]
    assert "particles at most" in _problems({"type": "motion", "elements": many})


def test_a_motion_scene_s_pictures_and_lotties_are_references_of_the_video():
    spec = {
        "title": "T",
        "scenes": [{"type": "motion", "elements": [{"kind": "image", "file_id": FILE}, {"kind": "lottie", "file_id": str(uuid.UUID(int=8))}]}],
    }
    assert referenced_ids(validate_video_spec(spec))["file"] == {FILE, str(uuid.UUID(int=8))}


# ── The compositions ──────────────────────────────────────────────────────


@pytest.mark.parametrize("personality", motion_design.PERSONALITIES)
@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920), (1080, 1080), (1080, 1350)])
def test_every_composition_lays_out_valid_elements_on_every_frame(personality, size):
    tokens = motion_design.resolve_motion({"personality": personality})
    for composition in MOTION_COMPOSITIONS:
        scene = _spec({"type": "motion", "composition": composition, "show_logo": True, **COMPOSITION_TEXTS}).scenes[0]
        drawn = motion_design.compose(scene, tokens, frame=motion_design.Frame(*size), theme={"logo": True}, length=6.0)
        assert drawn["elements"], composition
        assert drawn["reveal_end"] > 0
        ids = [element["id"] for element in drawn["elements"]]
        for element in drawn["elements"]:
            assert element["below"] is None or element["below"] in ids[: ids.index(element["id"])]
            if element["enter"]:
                assert element["enter"]["ease"] and element["enter"]["duration"] > 0


def test_a_logo_reveal_without_a_logo_spells_the_title_letter_by_letter():
    scene = _spec({"type": "motion", "composition": "logo_reveal", "title": "XTM One"}).scenes[0]
    drawn = motion_design.compose(scene, motion_design.resolve_motion(None), frame=motion_design.Frame(1920, 1080), theme={"logo": False}, length=4)
    wordmark = next(e for e in drawn["elements"] if e["id"] == "wordmark")
    assert wordmark["text"]["text"] == "XTM One" and wordmark["text"]["split"] == "letters"
    assert not drawn["brands"]
    with_logo = motion_design.compose(scene, motion_design.resolve_motion(None), frame=motion_design.Frame(1920, 1080), theme={"logo": True}, length=4)
    assert with_logo["brands"] and any(e["kind"] == "image" and e["image"]["src"] == "logo" for e in with_logo["elements"])


def test_a_title_moves_as_the_brand_moves():
    scene = _spec({"type": "motion", "composition": "kinetic_title", "title": "Moving brands"}).scenes[0]
    frame = motion_design.Frame(1920, 1080)
    energetic = motion_design.compose(scene, motion_design.resolve_motion({"personality": "energetic"}), frame=frame, theme={}, length=4)
    calm = motion_design.compose(scene, motion_design.resolve_motion({"personality": "calm"}), frame=frame, theme={}, length=4)
    title = {name: next(e for e in drawn["elements"] if e["id"] == "title") for name, drawn in (("energetic", energetic), ("calm", calm))}
    assert title["energetic"]["text"]["split"] == "letters" and title["energetic"]["enter"]["preset"] == "pop"
    assert title["energetic"]["enter"]["ease"] == "emphasis"
    assert title["calm"]["text"]["split"] == "lines" and title["calm"]["enter"]["preset"] == "mask_up"
    assert calm["reveal_end"] > energetic["reveal_end"]


def test_an_element_s_presets_take_the_brand_s_timings_and_its_exit_the_scene_s_end():
    scene = _spec(
        {"type": "motion", "elements": [{"kind": "text", "text": "Bye", "split": "words", "enter": {"preset": "rise"}, "exit": {"preset": "fade"}}]}
    ).scenes[0]
    tokens = motion_design.resolve_motion({"personality": "calm"})
    element = motion_design.compose(scene, tokens, frame=motion_design.Frame(1920, 1080), theme={}, length=5.0)["elements"][0]
    assert element["enter"]["duration"] == tokens["title"]["duration"] and element["enter"]["stagger"] == tokens["title"]["stagger"]
    assert element["exit"]["at"] == pytest.approx(5.0 - element["exit"]["duration"])
    assert element["exit"]["ease"] == "exit"


# ── Lottie files ──────────────────────────────────────────────────────────


def _lottie(**over) -> dict:
    return {"v": "5.7.4", "fr": 30, "ip": 0, "op": 60, "w": 200, "h": 200, "layers": [{"ty": 4, "ks": {}}], **over}


def test_a_lottie_is_played_only_when_it_loads_nothing_from_outside_itself():
    ok = motion_design.check_lottie(json.dumps(_lottie()).encode())
    assert ok.problem is None and ok.width == 200 and ok.seconds == 2.0
    embedded = _lottie(assets=[{"id": "img", "w": 10, "h": 10, "u": "", "p": "data:image/png;base64,iVBORw0KGgo=", "e": 1}])
    assert motion_design.check_lottie(json.dumps(embedded).encode()).problem is None
    linked = _lottie(assets=[{"id": "img", "w": 10, "h": 10, "u": "https://tracker.example/", "p": "pixel.png"}])
    assert "picture from outside itself" in motion_design.check_lottie(json.dumps(linked).encode()).problem
    font = _lottie(fonts={"list": [{"fName": "Brand", "fPath": "https://fonts.example/brand.css", "origin": 1}]})
    assert "font from outside itself" in motion_design.check_lottie(json.dumps(font).encode()).problem
    assert "not a Lottie animation" in motion_design.check_lottie(b"PK\x03\x04 a dotLottie archive").problem
    assert "MB at most" in motion_design.check_lottie(b" " * (motion_design.MAX_LOTTIE_BYTES + 1)).problem
    expressive = _lottie(layers=[{"ty": 4, "ks": {"p": {"a": 0, "k": [0, 0], "x": "wiggle(2, 20)"}}}])
    checked = motion_design.check_lottie(json.dumps(expressive).encode())
    assert checked.problem is None and "expressions are not played" in checked.notes[0]


def test_the_page_plays_lottie_with_the_light_build_and_no_expression_evaluator():
    from app.services.video_studio.compositor_page import LOTTIE_JS, page_html

    assert "</script" not in LOTTIE_JS.lower() and "eval(" not in LOTTIE_JS
    assert LOTTIE_JS in page_html("")


# ── Beats ─────────────────────────────────────────────────────────────────


def _clicks(bpm: float, offset: float, seconds: float = 30.0) -> np.ndarray:
    rate = beats.SAMPLE_RATE
    t = np.arange(int(rate * seconds)) / rate
    signal = 0.02 * np.random.default_rng(1).standard_normal(t.size)
    period = 60.0 / bpm
    burst = int(0.03 * rate)
    for k in range(int(seconds / period)):
        start = int((offset + k * period) * rate)
        if start + burst < t.size:
            signal[start : start + burst] += np.sin(2 * np.pi * 1800 * t[:burst]) * np.exp(-t[:burst] * 80) * (1.0 if k % 4 == 0 else 0.6)
    return signal.astype(np.float32)


@pytest.mark.parametrize(("bpm", "offset"), [(120, 0.31), (96, 0.12), (140, 0.05), (78, 0.4)])
def test_a_track_s_tempo_and_beat_are_heard_from_its_onsets(bpm, offset):
    found = beats.detect(_clicks(bpm, offset))
    assert found is not None
    tempo, first, _clarity = found
    assert tempo == pytest.approx(bpm, abs=0.5)
    period = 60.0 / tempo
    assert abs(((first - offset) / period + 0.5) % 1.0 - 0.5) * period < 0.02


def test_a_sound_with_no_steady_beat_has_none():
    rate = beats.SAMPLE_RATE
    assert beats.detect((0.1 * np.random.default_rng(2).standard_normal(rate * 20)).astype(np.float32)) is None
    t = np.arange(rate * 20) / rate
    assert beats.detect((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)) is None


def test_a_beat_grid_lands_on_its_beats_and_stops_where_the_track_does():
    grid = beats.BeatGrid(0.5, 0.25, until=3.0)
    assert grid.next(0.0) == 0.25 and grid.next(0.25) == 0.25 and grid.next(0.26) == 0.75
    assert grid.next(3.1) is None
    assert grid.within(1.0, 2.0) == [1.25, 1.75]
    assert beats.file_grid({"bpm": 120.0, "first": 0.3, "seconds": 10.0}, start=1.0) == beats.BeatGrid(0.5, 0.3, 9.0)
    assert beats.score_grid("pulse") is None or beats.score_grid("pulse").phase == 0.0


def test_a_built_in_track_s_beats_are_its_style_s_tempo():
    from app.services.video_studio.score import get_style, track_ids

    track = track_ids()[0]
    assert beats.score_grid(track).bpm == pytest.approx(get_style(track).bpm)


def _media_with_grid(grid: beats.BeatGrid) -> timeline.PlanMedia:
    media = timeline.PlanMedia()
    media.beats = grid
    return media


def test_cuts_land_on_the_beat_and_a_scene_only_ever_lengthens():
    scenes = [
        {"type": "statement", "lines": ["One"], "duration": 2.1},
        {"type": "motion", "composition": "kinetic_title", "title": "Two", "duration": 1.9},
        {"type": "statement", "lines": ["Three"], "duration": 2.0},
    ]
    off = _plan(_spec(*scenes))
    grid = beats.BeatGrid(0.5, 0.0)
    synced = _plan(_spec(*scenes, theme={"motion": {"beat_sync": "cuts"}}), _media_with_grid(grid))
    for before, after in zip(off.document["scenes"][:-1], synced.document["scenes"][:-1], strict=True):
        assert after["end"] - after["start"] >= before["end"] - before["start"] - 1e-6
        assert after["end"] / 0.5 == pytest.approx(round(after["end"] / 0.5), abs=1e-3)
    assert "beats" not in synced.document["scenes"][0]
    # Without music beats nothing moves.
    assert _plan(_spec(*scenes, theme={"motion": {"beat_sync": "cuts"}})).document["scenes"] == off.document["scenes"]


def test_with_beat_sync_all_the_builds_land_on_the_beats_too():
    scenes = [
        {"type": "motion", "composition": "list_build", "items": ["A", "B", "C"], "title": "List", "duration": 5.0},
        {"type": "statement", "lines": ["One", "Two"], "duration": 3.0},
    ]
    grid = beats.BeatGrid(0.5, 0.1)
    plan = _plan(_spec(*scenes, theme={"motion": {"beat_sync": "all"}}), _media_with_grid(grid))
    motion, statement = plan.document["scenes"]
    assert motion["beats"] and statement["beats"] and statement["beat_tolerance"] == pytest.approx(0.5 / 2, abs=2e-3)
    landed = [e["enter"]["at"] for e in motion["elements"] if e["enter"] and e["id"].startswith("item-")]
    local = [round(b, 4) for b in motion["beats"]]
    assert all(at in local for at in landed)


def test_a_clip_held_longer_for_its_beat_holds_its_last_frame():
    clip_id = str(uuid.UUID(int=9))
    spec = _spec(
        {"type": "clip", "clip_id": clip_id, "audio": False},
        {"type": "statement", "lines": ["After"], "duration": 2.0},
        theme={"motion": {"beat_sync": "cuts"}},
    )
    media = _media_with_grid(beats.BeatGrid(0.7, 0.0))
    media.clips[spec.scenes[0].id] = timeline.ClipMedia(duration=2.0, width=1920, height=1080, fps=30, frame_count=60)
    scene = _plan(spec, media).document["scenes"][0]
    assert scene["end"] == pytest.approx(2.1)
    assert scene["time_map"][-1] == [pytest.approx(2.1), pytest.approx(2.0)]


def test_the_planner_draws_a_motion_scene_s_elements_and_says_what_it_leaves_out():
    spec = _spec(
        {
            "type": "motion",
            "elements": [
                {"kind": "image", "file_id": FILE},
                {"kind": "lottie", "file_id": FILE},
                {"kind": "image", "asset": "logo"},
                {"kind": "text", "text": "Kept"},
            ],
        }
    )
    media = timeline.PlanMedia()
    media.overlays_gathered = media.lotties_gathered = True
    plan = _plan(spec, media, logo=False)
    kept = [e["kind"] for e in plan.document["scenes"][0]["elements"]]
    assert kept == ["text"]
    assert any("picture could not be read" in note for note in plan.notes)
    assert any("Lottie element" in note for note in plan.notes)
    assert any("has no logo" in note for note in plan.notes)


def test_a_figure_stands_in_space_when_asked():
    figure = {"type": "figure", "block_spec": {"type": "process_flow", "steps": [{"title": "a"}, {"title": "b"}]}}
    assert "perspective" not in _plan(_spec(figure)).document["scenes"][0]
    assert _plan(_spec({**figure, "perspective": "orbit"})).document["scenes"][0]["perspective"] == "orbit"


def test_a_motion_scene_is_named_by_what_it_writes():
    spec = _spec({"type": "motion", "composition": "stat_counter", "figure": "6,000+"})
    chapters = timeline.chapters(spec, _plan(spec).document)
    assert chapters[0]["title"] == "6,000+" and chapters[0]["type"] == "motion"


# ── Motion blur ───────────────────────────────────────────────────────────


def test_motion_blur_is_drawn_at_high_and_ultra_only():
    on = {"motion_blur": "on"}
    assert motion_design.blur_samples(on, "ultra") == motion_design.blur_samples(on, "high") == 3
    assert motion_design.blur_samples(on, "standard") == motion_design.blur_samples(on, "draft") == 1
    assert motion_design.blur_samples({"motion_blur": "off"}, "ultra") == 1
    assert motion_design.blur_samples(None, "ultra") == 1
    assert motion_design.blur_offsets(1) == [0.0]
    offsets = motion_design.blur_offsets(3)
    assert offsets[1] == 0.0 and offsets[0] == pytest.approx(-offsets[2]) and offsets[2] - offsets[0] < motion_design.BLUR_SHUTTER


def test_the_encoder_averages_each_frame_s_sub_frames():
    assert motion_blur_filter(1) == ""
    assert motion_blur_filter(3) == "tmix=frames=3:weights='1 1 1',select='eq(mod(n\\,3)\\,2)',setpts=PTS-STARTPTS,"


def test_the_pre_flight_counts_the_sub_frames_a_blurred_render_draws():
    spec = _spec({"type": "statement", "lines": ["Blur"], "duration": 2.0}, theme={"motion": {"motion_blur": "on"}})
    document = _plan(spec).document
    sharp = render_preflight.FrameKinds.of(document, quality="standard")
    blurred = render_preflight.FrameKinds.of(document, quality="ultra")
    assert blurred.samples == 3 and sharp.samples == 1
    assert sum(blurred.counts().values()) == 3 * sum(sharp.counts().values()) == 3 * document["frames"]


def test_frames_are_begun_where_chromium_offers_it(monkeypatch):
    import sys

    from app.core.config import settings
    from app.services.video_studio import compositor

    monkeypatch.setattr(settings, "video_studio_frame_capture", "auto")
    monkeypatch.setattr(sys, "platform", "linux")
    assert compositor.frame_capture_mode() == "begin_frame"
    monkeypatch.setattr(sys, "platform", "win32")
    assert compositor.frame_capture_mode() == "screenshot"
    monkeypatch.setattr(settings, "video_studio_frame_capture", "screenshot")
    monkeypatch.setattr(sys, "platform", "linux")
    assert compositor.frame_capture_mode() == "screenshot"
    monkeypatch.setattr(settings, "video_studio_frame_capture", "begin_frame")
    assert compositor.frame_capture_mode() == "begin_frame"


class _BegunPage:
    """A page driven frame by frame: each begin answers the next of *answers*; a screenshot is never possible."""

    def __init__(self, answers: list[dict]):
        self.answers, self.asked = list(answers), []

    async def send(self, method: str, params: dict) -> dict:
        self.asked.append(method)
        assert method == "HeadlessExperimental.beginFrame", "a page whose frames are begun never paints by itself"
        return dict(self.answers.pop(0))


async def test_a_begun_page_is_begun_again_until_its_renderer_gives_a_picture():
    import base64

    from app.services.video_studio import compositor

    first, second = base64.b64encode(b"first").decode(), base64.b64encode(b"second").decode()
    page = compositor._Page(1, None)  # type: ignore[arg-type]
    page.begin_frame = True
    # Starting up, its renderer answers without a picture, damaged or not.
    page.cdp = _BegunPage([{"hasDamage": False}, {"hasDamage": True}, {"hasDamage": True, "screenshotData": first}])
    assert await compositor._capture(page, {}) == b"first" and len(page.cdp.asked) == 3
    # Nothing changed: the frame is the one before; damaged without a picture, it is begun again.
    page.cdp = _BegunPage([{"hasDamage": False}])
    assert await compositor._capture(page, {}) == b"first"
    page.cdp = _BegunPage([{"hasDamage": True}, {"hasDamage": True, "screenshotData": second}])
    assert await compositor._capture(page, {}) == b"second"
    page.cdp = _BegunPage([{"hasDamage": True}] * compositor._PICTURE_BEGINS)
    with pytest.raises(RuntimeError, match="frames begun without a picture"):
        await compositor._capture(page, {})


# ── The templates' motion ─────────────────────────────────────────────────


def _video_templates() -> list[dict]:
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    return [data for data in BUILTIN_TEMPLATE_CONTAINERS if data["kind"] == "video"]


def test_every_built_in_video_template_moves_its_own_way():
    seen = set()
    for data in _video_templates():
        style = data["extra_metadata"]["video"]
        assert templates.validate_style(style)[1] == [], data["slug"]
        motion = style["defaults"]["theme"]["motion"]
        assert motion["personality"] in motion_design.PERSONALITIES, data["slug"]
        seen.add(json.dumps(motion, sort_keys=True))
    # Not one motion for all: each brand family has its own.
    assert len(seen) >= 6


def test_the_filigran_brand_templates_lock_their_personality():
    locked = {data["slug"]: data["extra_metadata"]["video"]["locked"] for data in _video_templates()}
    for slug in ("filigran-video", "thread-video", "filigran-brand-film", "filigran-event-loop"):
        assert "theme.motion.personality" in locked[slug], slug


def test_a_template_s_motion_reaches_the_effective_spec_and_its_lock_holds():
    data = next(d for d in _video_templates() if d["slug"] == "filigran-brand-film")
    style = templates.parse_style(data["extra_metadata"]["video"])
    template = templates.VideoTemplate(id=str(uuid.uuid4()), name="Filigran Brand Film", kind="video", style=style)
    authored = {"title": "Film", "theme": {"motion": {"personality": "playful", "speed": 1.2}}, "scenes": [{"type": "statement", "lines": ["Hi"]}]}
    applied = templates.apply_template(authored, template)
    motion = applied.spec["theme"]["motion"]
    assert motion["personality"] == "cinematic" and motion["speed"] == 1.2 and motion["motion_blur"] == "on"
    assert any("theme.motion.personality" in note for note in applied.notes)
