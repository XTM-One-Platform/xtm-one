"""Five generic gaps of the scene previews, their queue, bezel quads and template watermarks.

What is proven, without a worker or a database:

* a template's ``defaults.theme.watermark`` fills an unset watermark, the
  template summary names it, and a save says when a video draws none while
  its template draws one - never when the template draws none either;
* a scene showing a figure, a recording or a screen insert is previewed at
  1080p (``standard``), any other at a 720p draft, and a 1080p preview's
  memory ceiling is sized to its frames;
* ``quad: auto`` on a static insert draws nothing until the render finds it,
  is refused on a tracked insert (several in a scene each take a screen of
  their own: tests/test_video_studio_quad_auto_screens.py), stands on the whole
  footage while it is gathered and planned, and is found as the bezel of the
  flat surface the first footage frame shows - ``None`` when there is none;
* a scene preview's verdict is its own scene's: a neighbour's failing
  finding its own drawing reported is left out, another becomes a warning
  naming that neighbour;
* the preview lane is dealt round-robin by video, the previews a video
  already runs counted, and a scene whose content is unchanged keeps its
  passing preview after an edit of the theme or the music alone - a failed
  one never.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_studio import projects, render_checks, render_preflight, surface_refine, templates, timeline
from app.services.video_studio.preview_lane import dealt
from app.services.video_studio.render_checks import FAIL, WARN, Checks, Finding
from app.services.video_studio.spec import VideoSpecError, parse_video_spec, validate_video_spec

CLIP = "00000000-0000-0000-0000-000000006214"


def _row(watermark: str | None) -> SimpleNamespace:
    theme = {"mode": "dark", "background": "aurora", **({"watermark": watermark} if watermark else {})}
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Brand Film",
        kind="video",
        description=None,
        tags=[],
        theme_config={},
        extra_metadata={"video": {"defaults": {"theme": theme}}, "supported_schemes": ["dark"]},
    )


def _authored(theme: dict | None = None) -> dict:
    return {
        "title": "Launch",
        **({"theme": theme} if theme is not None else {}),
        "scenes": [{"id": "s01", "type": "statement", "lines": ["One platform."]}],
    }


# ── 1. The watermark is the template's ───────────────────────────────────


def test_an_unset_watermark_is_the_templates_corner_and_the_summary_names_it():
    row = _row("bottom_right")
    template = templates.VideoTemplate.from_container(row)
    applied = templates.apply_template(_authored(), template)
    assert applied.spec["theme"]["watermark"] == "bottom_right"
    assert templates.template_summary(row)["watermark"] == "bottom_right"
    assert templates.template_summary(_row(None))["watermark"] is None
    assert templates.watermark_note(_authored(), applied.spec, template) is None


def test_a_save_says_a_video_draws_no_watermark_while_its_template_draws_one():
    template = templates.VideoTemplate.from_container(_row("bottom_right"))
    authored = _authored({"watermark": "none"})
    applied = templates.apply_template(authored, template)
    assert applied.spec["theme"]["watermark"] == "none"
    note = templates.watermark_note(authored, applied.spec, template)
    assert note is not None
    assert "the spec sets theme.watermark: none" in note and "bottom right" in note and "Brand Film" in note
    # A template drawing no watermark has nothing to say, and neither has free style.
    bare = templates.VideoTemplate.from_container(_row("none"))
    assert templates.watermark_note(authored, templates.apply_template(authored, bare).spec, bare) is None
    assert templates.watermark_note(authored, applied.spec, None) is None


# ── 2. A preview is drawn at the size its scene is judged at ─────────────


def _effective(scene: dict) -> dict:
    return validate_video_spec({"title": "Quality", "freestyle": True, "scenes": [scene]})


def test_a_scene_showing_a_figure_a_recording_or_an_insert_is_previewed_at_1080p():
    insert = {
        "type": "screen_insert",
        "source": {"clip_id": CLIP},
        "quad": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
    }
    shot = _effective({"id": "s", "type": "clip", "clip_id": CLIP, "overlays": [insert]})
    assert projects.preview_quality(shot, "s") == ("standard", "it shows a screen insert")
    recording = {"scenes": [{"id": "r", "type": "recording", "take_id": CLIP}]}
    assert projects.preview_quality(recording, "r") == ("standard", "it shows a recording")
    figure = {"scenes": [{"id": "f", "type": "figure"}]}
    assert projects.preview_quality(figure, "f") == ("standard", "it shows a figure")
    statement = _effective({"id": "t", "type": "statement", "lines": ["Hello"]})
    assert projects.preview_quality(statement, "t") == ("draft", None)


def test_a_1080p_preview_has_the_memory_ceiling_of_its_frames(monkeypatch):
    from app.core.config import settings
    from app.services.video_studio import render_process, resources

    monkeypatch.setattr(settings, "video_studio_preview_memory_mb", 0)
    monkeypatch.setattr(settings, "video_studio_render_memory_mb", 64_000)
    monkeypatch.setattr(resources, "memory_limit", lambda: 0)
    draft = render_process.preview_memory_ceiling()
    standard = render_process.preview_memory_ceiling(quality="standard")
    assert standard > draft
    assert render_process.preview_memory_ceiling(quality="ultra") == draft
    row = SimpleNamespace(scene_id="s", quality="standard")
    assert render_process.ceiling_of(row) == standard


def test_the_scene_render_body_takes_a_preview_quality_only():
    from pydantic import ValidationError

    from app.api.v1.schemas.video_studio import SceneRenderRequest

    assert SceneRenderRequest(scene="s03", quality="standard").quality == "standard"
    assert SceneRenderRequest(scene=3).quality is None
    with pytest.raises(ValidationError):
        SceneRenderRequest(scene="s03", quality="ultra")


# ── 3. quad: auto ────────────────────────────────────────────────────────


def _auto_spec(**overlay) -> dict:
    return {
        "title": "Auto",
        "freestyle": True,
        "scenes": [
            {
                "id": "s",
                "type": "clip",
                "clip_id": CLIP,
                "overlays": [{"type": "screen_insert", "source": {"clip_id": CLIP}, "quad": "auto", **overlay}],
            }
        ],
    }


def test_an_auto_quad_draws_nothing_until_it_is_found_and_stands_on_the_footage_meanwhile():
    spec = parse_video_spec(validate_video_spec(_auto_spec()))
    overlay = spec.scenes[0].overlays[0]
    assert overlay.auto_quad and overlay.insert_panes() == []
    # Its source is still what the scene shows: the gate waits for it to be ready.
    assert [source.clip_id for source in overlay.shown_sources] == [CLIP]
    assert timeline.auto_quads(spec) == [("s", 0)]
    assert timeline.insert_sources(spec) == {}
    staged = timeline.insert_sources(timeline.with_quads(spec, {}, stand_in=True))
    assert [part.quad for part in staged.values()] == [timeline.STAND_IN_QUAD]
    found = ((0.25, 0.2), (0.75, 0.2), (0.75, 0.8), (0.25, 0.8))
    resolved = timeline.with_quads(spec, {("s", 0): found})
    assert [part.quad for part in timeline.insert_sources(resolved).values()] == [found]
    # The memory estimate counts it before it is found.
    assert render_preflight.InsertLoad.of_spec(spec).videos == 1


def test_an_auto_quad_is_refused_on_a_tracked_insert_and_taken_twice_in_a_scene():
    with pytest.raises(VideoSpecError, match="tracked insert"):
        validate_video_spec(_auto_spec(track=True))
    # Two inserts left to the footage: the save hands each a screen of its own (``insert_bezels.assign_screens``).
    twice = _auto_spec()
    twice["scenes"][0]["overlays"].append(dict(twice["scenes"][0]["overlays"][0]))
    spec = parse_video_spec(validate_video_spec(twice))
    assert timeline.auto_quads(spec) == [("s", 0), ("s", 1)]


def _room(screen: bool = True, green: tuple[int, int, int] = (0, 177, 64)):
    """A 1280x720 frame: a dark room, a black bezel and, inside it, a flat green screen from (320, 170) to (960, 550)."""
    import numpy as np

    frame = np.full((720, 1280, 3), 46, dtype=np.uint8)
    frame[150:571, 300:981] = (18, 18, 20)
    if screen:
        frame[170:550, 320:960] = green
    return frame


def test_the_bezel_of_the_flat_screen_on_the_first_frame_is_the_quad():
    from app.services.video_studio import screens

    expected = ((320 / 1280, 170 / 720), (960 / 1280, 170 / 720), (960 / 1280, 550 / 720), (320 / 1280, 550 / 720))
    named = screens.Key("green", (0, 177, 64))
    # A video model's own green is keyed as the shade it shows, as the render keys it.
    for frame, key in ((_room(), None), (_room(), named), (_room(green=(32, 255, 30)), named)):
        quad = surface_refine.found_quad(frame, key)
        assert quad is not None
        for (x, y), (ex, ey) in zip(quad, expected, strict=True):
            assert abs(x - ex) < 0.004 and abs(y - ey) < 0.006, (key, quad)


def test_no_flat_screen_on_the_first_frame_is_no_bezel():
    from app.services.video_studio import screens

    assert surface_refine.found_quad(_room(screen=False)) is None
    # A key of another colour finds no surface of its own.
    assert surface_refine.found_quad(_room(), screens.Key("magenta", (230, 0, 210))) is None


# ── 4. A preview's verdict is its own scene's ────────────────────────────


def _insert_finding(scene_id: str, kind: str = render_checks.INSERT_ALIGNMENT) -> Finding:
    return Finding(kind, 0.2, 1.0, scene_id, f"Scene {scene_id}: its screen insert 0 is off its bezel", FAIL, {"insert": f"insert/{scene_id}.0"})


def test_a_neighbours_insert_never_fails_a_preview():
    checks = Checks(
        findings=[_insert_finding("s03"), _insert_finding("s05", render_checks.INSERT_FLICKER), _insert_finding("s04")],
        inserts=[{"key": "insert/s03.0", "scene_id": "s03"}, {"key": "insert/s04.0", "scene_id": "s04"}],
    )
    left_out = render_checks.settle_neighbours(checks, "s04", {"s03": {render_checks.INSERT_ALIGNMENT}})
    # s03 reported its own alignment: left out, said. s05's flicker is new: a warning on s05. s04's own stays.
    assert [f.scene_id for f in left_out] == ["s03"]
    by_scene = {f.scene_id: f for f in checks.findings}
    assert set(by_scene) == {"s04", "s05"}
    assert by_scene["s05"].severity == WARN and by_scene["s05"].data["neighbour"] is True
    assert by_scene["s05"].message.startswith("on scene s05, drawn beside this one")
    assert by_scene["s04"].severity == FAIL
    assert any("Scene s03's insert_alignment" in note for note in checks.notes)
    assert [r.get("neighbour") for r in checks.inserts] == [True, None]
    assert render_checks.scene_passed(checks.as_dict(), "s04") is False

    alone = Checks(findings=[_insert_finding("s03")])
    render_checks.settle_neighbours(alone, "s04", {})
    assert alone.status == render_checks.FINDINGS and render_checks.scene_passed(alone.as_dict(), "s04")


def test_what_each_scene_reported_is_read_off_its_drawing_as_it_is_now():
    failed = {"findings": [_insert_finding("s03").as_dict()]}
    seen = render_checks.record_seen(None, {"s03": "d3", "s04": "d4"}, "r1", checks=failed)
    assert render_checks.reported_pictures(seen, {"s03": "d3", "s04": "d4"}) == {"s03": {render_checks.INSERT_ALIGNMENT}}
    # Drawn otherwise since: what it reported then is no report of it now.
    assert render_checks.reported_pictures(seen, {"s03": "other"}) == {}


# ── 5. A fair lane, and previews kept across a theme edit ────────────────


def test_the_preview_lane_is_dealt_round_robin_by_video():
    queue = [(f"a{n}", "A") for n in range(1, 11)] + [("b1", "B"), ("c1", "C"), ("c2", "C")]
    assert dealt(queue, {}).order[:6] == ["a1", "b1", "c1", "a2", "c2", "a3"]
    # A video already running its previews goes after one running none.
    assert dealt(queue, {"A": 2}).order[:5] == ["b1", "c1", "c2", "a1", "a2"]
    # One video alone keeps its order.
    assert dealt([("x", "A"), ("y", "A"), ("z", "A")], {}).order == ["x", "y", "z"]


def _video(*, watermark: str = "bottom_right", music: dict | None = None, line: str = "One platform.") -> dict:
    return validate_video_spec(
        {
            "title": "Kept",
            "freestyle": True,
            "theme": {"watermark": watermark},
            **({"music": music} if music else {}),
            "scenes": [
                {"id": "s01", "type": "title", "title": "Hello"},
                {"id": "s02", "type": "statement", "lines": [line]},
                {"id": "s03", "type": "statement", "lines": ["Every alert closed."]},
                {"id": "s04", "type": "outro", "title": "Thanks"},
            ],
        }
    )


def _marks(effective: dict) -> render_checks.SceneDigests:
    return asyncio.run(render_checks.digest_scenes(None, effective))


def test_a_theme_or_music_edit_keeps_every_passing_preview_and_a_scene_edit_does_not():
    before = _marks(_video())
    seen: dict = {}
    for scene, digest in before.whole.items():
        seen = render_checks.record_seen(seen, {scene: digest}, uuid.uuid4(), checks={"findings": []}, content=before.content)
    moved = _marks(_video(watermark="top_left"))
    assert all(moved.whole[s] != before.whole[s] for s in before.whole)
    assert moved.content == before.content
    assert render_checks.changed_scenes(seen, moved.whole, moved.content) == []
    assert render_checks.passed_scenes(seen, moved.whole, moved.content) == set(before.whole)
    assert render_checks.kept_scenes(seen, moved.whole, moved.content) == list(before.whole)
    scored = _marks(_video(music={"track": "signal"}))
    assert scored.content == before.content and render_checks.changed_scenes(seen, scored.whole, scored.content) == []
    # Without the content digests (a drawing recorded before them), the theme edit is a change, as before.
    assert render_checks.changed_scenes(seen, moved.whole) == list(before.whole)
    # A scene edited changes its own content and its neighbours', never the others'.
    edited = _marks(_video(watermark="top_left", line="Two platforms."))
    assert render_checks.changed_scenes(seen, edited.whole, edited.content) == ["s01", "s02", "s03"]


def test_a_failed_preview_is_never_kept_across_a_theme_edit():
    before = _marks(_video())
    failing = {"findings": [{"kind": "stutter", "severity": "fail", "at": 1.0, "scene_id": "s02"}]}
    seen = render_checks.record_seen(None, {"s02": before.whole["s02"]}, "r1", checks=failing, content=before.content)
    moved = _marks(_video(watermark="none"))
    assert render_checks.changed_scenes(seen, moved.whole, moved.content) == ["s02"]
    assert render_checks.passed_scenes(seen, moved.whole, moved.content) == set()
    assert render_checks.kept_scenes(seen, moved.whole, moved.content) == []
