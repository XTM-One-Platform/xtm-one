"""The gaps a live-action brand film made of AI shots, recordings and statements hit.

Synthetic content only: neutral titles, solid-colour pictures, made-up names.

* a picture placed on a scene: the ``image`` overlay - a file or a brand mark
  by its role - sized by the planner, placed like a badge, preset by a
  template, left out (and said) when its picture cannot be read;
* a cut takes no duration, a named move no pace (the edit path included);
* a render's poster is a settled frame, never a dissolve;
* a track shorter than the video is said when the video is saved, the render
  names the track's length against the video's, and a ready AI track is
  composed again in place at a new length (``refit``);
* a picture a job tool shows is attached once per conversation, then named;
* real pictures drawn inside a picture, flat or in perspective;
* a page's interactive names, and the closest ones to what a step asked for.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from app.models.file_attachment import FileAttachment
from app.models.video_studio import JOB_PENDING, JOB_READY, JOB_RUNNING
from app.services.video_studio import capture, music, pictures, projects, templates, timeline
from app.services.video_studio.spec import (
    Music,
    Overlay,
    Transition,
    authored_spec,
    parse_video_spec,
    referenced_ids,
    validate_video_spec,
)

LOGO = "00000000-0000-0000-0000-00000000f001"
TRACK = "00000000-0000-0000-0000-00000000f002"
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-00000000c0c0")
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")


def _spec(*scenes: dict, **top) -> dict:
    return {"title": "Synthetic film", "freestyle": True, "scenes": list(scenes), **top}


# ── 1. A picture placed on a scene ────────────────────────────────────────


def test_an_image_overlay_draws_one_picture_a_file_or_a_brand_mark():
    mark = Overlay.model_validate({"type": "image", "asset": "logo_mark", "position": "top_right", "width": 0.08})
    assert (mark.asset, mark.file_id, mark.width, mark.text) == ("logo_mark", None, 0.08, "")
    picture = Overlay.model_validate({"type": "image", "file_id": f"[[FILE:{LOGO}]]", "opacity": 0.9})
    assert picture.file_id == LOGO
    with pytest.raises(ValidationError, match="draws one picture"):
        Overlay.model_validate({"type": "image"})
    with pytest.raises(ValidationError, match="draws one picture"):
        Overlay.model_validate({"type": "image", "file_id": LOGO, "asset": "logo"})
    with pytest.raises(ValidationError):
        Overlay.model_validate({"type": "image", "asset": "banner"})


def test_the_fields_a_kind_of_overlay_does_not_take_are_dropped():
    # A picture takes no style or second line; a text overlay no picture, width or opacity.
    picture = Overlay.model_validate({"type": "image", "asset": "logo", "style": "accent", "subtext": "x"})
    assert set(picture.model_dump(exclude_unset=True)) == {"type", "asset"}
    badge = Overlay.model_validate({"type": "badge", "text": "New", "file_id": LOGO, "width": 0.2, "opacity": 0.5})
    assert set(badge.model_dump(exclude_unset=True)) == {"type", "text"}
    # An unknown field is still refused, named.
    with pytest.raises(ValidationError, match="Extra inputs"):
        Overlay.model_validate({"type": "image", "asset": "logo", "scale": 2})


def test_an_image_overlay_file_is_a_reference_of_the_video():
    spec = validate_video_spec(
        _spec({"type": "statement", "lines": ["A line"], "overlays": [{"type": "image", "file_id": LOGO}]})
    )
    assert LOGO in referenced_ids(spec)["file"]


def _overlay_plan(overlay: dict, media: timeline.PlanMedia, *, ratio: str = "16:9") -> timeline.TimelinePlan:
    spec = parse_video_spec(
        validate_video_spec(
            _spec({"id": "s", "type": "statement", "lines": ["A line"], "duration": 5, "overlays": [overlay]}),
        )
        | {"format": {"aspect_ratio": ratio}}
    )
    return timeline.plan(spec, media, fps=30, theme={})


def test_the_planner_sizes_an_image_overlay_in_design_pixels_from_its_picture():
    key = f"overlay/{LOGO}"
    media = timeline.PlanMedia(overlays={key: timeline.ImageMedia(400, 100)}, overlays_gathered=True)
    plan = _overlay_plan({"type": "image", "file_id": LOGO, "width": 0.1, "opacity": 0.8, "start": 1}, media)
    drawn = plan.document["scenes"][0]["overlays"][0]
    assert (drawn["type"], drawn["src"], drawn["w"], drawn["h"], drawn["opacity"]) == ("image", key, 192.0, 48.0, 0.8)
    assert (drawn["t0"], drawn["t1"]) == (1.0, 4.0)
    # A width above 1 is pixels of the design frame; a portrait frame is 1080 wide.
    wide = _overlay_plan({"type": "image", "file_id": LOGO, "width": 300}, media)
    assert wide.document["scenes"][0]["overlays"][0]["w"] == 300.0
    tall = _overlay_plan({"type": "image", "file_id": LOGO}, media, ratio="9:16")
    assert tall.document["scenes"][0]["overlays"][0]["w"] == round(0.12 * 1080, 1)


def test_an_image_overlay_whose_picture_was_not_found_is_left_out_and_said():
    gathered = timeline.PlanMedia(overlays_gathered=True)
    plan = _overlay_plan({"type": "image", "asset": "logo_mark"}, gathered)
    assert plan.document["scenes"][0]["overlays"] == []
    assert any("image overlay is left out" in note and "logo_mark" in note for note in plan.notes)
    # An estimate gathers nothing: the overlay is planned, its picture square until known.
    estimate = _overlay_plan({"type": "image", "asset": "logo_mark"}, timeline.PlanMedia())
    drawn = estimate.document["scenes"][0]["overlays"][0]
    assert drawn["w"] == drawn["h"] and drawn["src"] == "overlay/logo_mark"


def test_a_brand_mark_is_the_templates_by_its_role():
    spec = parse_video_spec(
        validate_video_spec(
            _spec(
                {
                    "type": "statement",
                    "lines": ["A line"],
                    "overlays": [
                        {"type": "image", "asset": "logo_mark"},
                        {"type": "image", "asset": "logo"},
                        {"type": "image", "asset": "logo_light"},
                        {"type": "image", "file_id": LOGO},
                    ],
                }
            )
        )
    )
    palette = {"logo_file_id": "logo-dark", "mark_file_id": "mark"}
    assert timeline.overlay_file_ids(spec, palette) == {
        "overlay/logo_mark": "mark",
        "overlay/logo": "logo-dark",
        "overlay/logo_light": "logo-dark",
        f"overlay/{LOGO}": LOGO,
    }
    assert timeline.overlay_file_ids(spec, {})["overlay/logo_mark"] is None


def _template(style: dict) -> templates.VideoTemplate:
    return templates.VideoTemplate(
        id="t", name="Brand", kind="video", style=templates.VideoTemplateStyle.model_validate(style)
    )


def test_a_template_adds_its_brand_mark_before_a_scenes_own_overlays_unless_it_draws_it():
    mark = {"type": "image", "asset": "logo_mark", "position": "top_right", "width": 0.06, "duration": 60}
    template = _template(
        {"scene_defaults": {"clip": {"overlays": [mark], "fit": "cover"}}, "overlay_defaults": {"image": {"opacity": 0.85}}}
    )
    clip = "00000000-0000-0000-0000-0000000000c1"
    applied = templates.apply_template(
        _spec(
            {"id": "a", "type": "clip", "clip_id": clip},
            {"id": "b", "type": "clip", "clip_id": clip, "overlays": [{"type": "badge", "text": "New"}]},
            {
                "id": "c",
                "type": "clip",
                "clip_id": clip,
                "overlays": [{"type": "image", "asset": "logo_mark", "position": "bottom_left"}],
            },
            {"id": "d", "type": "statement", "lines": ["Words only"]},
        )
        | {"freestyle": False},
        template,
    )
    scenes = {scene["id"]: scene for scene in applied.spec["scenes"]}
    assert [(o["type"], o.get("position")) for o in scenes["a"]["overlays"]] == [("image", "top_right")]
    assert scenes["a"]["overlays"][0]["opacity"] == 0.85 and scenes["a"]["fit"] == "cover"
    assert [o["type"] for o in scenes["b"]["overlays"]] == ["image", "badge"]
    assert [o.get("position") for o in scenes["c"]["overlays"]] == ["bottom_left"]
    assert not scenes["d"].get("overlays")


def test_a_template_presets_brand_marks_by_role_only_and_an_image_overlays_look():
    _rules, problems = templates.validate_style(
        {"scene_defaults": {"clip": {"overlays": [{"type": "image", "file_id": LOGO}]}}}
    )
    assert any("brand marks only" in problem for problem in problems)
    _rules, problems = templates.validate_style(
        {"scene_defaults": {"clip": {"overlays": [{"type": "badge", "text": "Words"}]}}}
    )
    assert any("brand marks only" in problem for problem in problems)
    rules, problems = templates.validate_style(
        {
            "scene_defaults": {"clip": {"overlays": [{"type": "image", "asset": "logo_mark", "width": 0.06}]}},
            "overlay_defaults": {"image": {"width": 0.1, "opacity": 0.9, "position": "top_left"}},
        }
    )
    assert problems == [] and rules["overlay_defaults"]["image"]["opacity"] == 0.9


def test_the_compositor_places_a_picture_like_a_badge_and_audits_it_as_its_own_group():
    from app.services.video_studio.compositor_page import PAGE_JS

    assert "o.type === 'badge' || o.type === 'image'" in PAGE_JS
    assert "['ovimg', 'image overlay']" in PAGE_JS and ".ovimg';" in PAGE_JS


# ── 9. A cut takes no duration ────────────────────────────────────────────


def test_a_cut_takes_no_duration_and_a_named_move_no_pace():
    def written(raw: dict) -> dict:
        return Transition.model_validate(raw).model_dump(exclude_unset=True)

    assert written({"type": "cut", "duration": 0.7, "pace": "dynamic"}) == {"type": "cut"}
    assert written({"type": "crossfade", "duration": 0.7, "pace": "dynamic"}) == {"type": "crossfade", "duration": 0.7}
    assert written({"type": "auto", "duration": 0.7, "pace": "classic"}) == {
        "type": "auto",
        "duration": 0.7,
        "pace": "classic",
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        Transition.model_validate({"type": "cut", "durations": 1})


def test_setting_a_cut_over_an_auto_with_a_duration_leaves_none_and_the_line_writes_none():
    stored = authored_spec(
        _spec(
            {"id": "one", "type": "statement", "lines": ["One"], "duration": 3},
            {"id": "two", "type": "statement", "lines": ["Two"], "duration": 3},
            default_transition={"type": "auto", "duration": 0.7},
        )
    )
    edited, _lines = projects.apply_operations(
        stored, [{"op": "update", "fields": {"default_transition": {"type": "cut"}}}]
    )
    assert authored_spec(edited)["default_transition"] == {"type": "cut"}
    line = asyncio.run(projects.transitions_line(None, validate_video_spec(edited)))
    assert line == "Transitions into each scene, as planned now: two: cut."


# ── 7. The poster is a settled frame ──────────────────────────────────────


def _planned(*scenes: dict, **top) -> dict:
    spec = parse_video_spec(validate_video_spec(_spec(*scenes, **top)))
    return timeline.plan(spec, timeline.PlanMedia(), fps=30, theme={}).document


def test_the_poster_is_taken_out_of_both_transitions_of_its_scene():
    document = _planned(
        {"id": "open", "type": "title", "title": "Open", "duration": 3},
        {"id": "a", "type": "statement", "lines": ["First"], "duration": 4, "transition": {"type": "crossfade", "duration": 0.8}},
        {"id": "b", "type": "statement", "lines": ["Second"], "duration": 4, "transition": {"type": "crossfade", "duration": 0.8}},
    )
    _open, a, b = document["scenes"]
    at = timeline.poster_time(document)
    in_end = a["start"] + a["transition_in"]["duration"]
    assert a["transition_in"]["type"] == "crossfade" and b["transition_in"]["type"] == "crossfade"
    assert in_end < at < b["start"]
    assert at == pytest.approx(in_end + timeline.POSTER_SETTLE_SECONDS)


def test_a_poster_skips_the_title_cards_and_the_fades():
    only_cards = _planned({"type": "title", "title": "Card", "duration": 3})
    assert timeline.poster_time(only_cards) == pytest.approx(only_cards["fade_in"])
    # A first scene cut in at 0 s is still taken once the fade from black is over.
    first = _planned({"type": "statement", "lines": ["Words"], "duration": 4})
    assert timeline.poster_time(first) == pytest.approx(first["fade_in"] + timeline.POSTER_SETTLE_SECONDS)
    short = _planned(
        {"type": "statement", "lines": ["Short"], "duration": 1.2},
        {"type": "statement", "lines": ["Next"], "duration": 4, "transition": {"type": "crossfade", "duration": 0.5}},
        fade_in=False,
    )
    settled_until = short["scenes"][1]["start"]
    assert 0 < timeline.poster_time(short) < settled_until


# ── 6. The music follows the timeline's length ────────────────────────────


class _Rows:
    def __init__(self, row=None) -> None:
        self.row = row

    async def get(self, _model, _key):
        return self.row


def _track(**fields):
    return SimpleNamespace(**{"status": JOB_READY, "duration_ms": 142_000, "params": {}, **fields})


def test_a_track_shorter_than_the_video_is_said_with_where_it_ends_or_loops():
    ends = asyncio.run(music.length_note(_Rows(_track()), Music(music_id=TRACK, loop=False), 183.0))
    assert ends.startswith("Music ends at 142 s of 183 s (loop: false)") and "refit: true" in ends
    # Played once, never looped.
    assert "it fades out at 142 s, leaving the video's last 41 s without music" in ends and "repeat" not in ends
    # An AI track the video outgrew by more than 2 s is the save's quality check's warning, not this note.
    assert asyncio.run(music.length_note(_Rows(_track()), Music(music_id=TRACK), 183.0)) is None
    late_start = asyncio.run(music.length_note(_Rows(_track(duration_ms=200_000)), Music(music_id=TRACK, start=20), 181.5))
    assert late_start.startswith("Music loops at 180 s of 182 s")
    composing = _track(status=JOB_RUNNING, duration_ms=None, params={"duration_seconds": 120})
    ends_composed = asyncio.run(music.length_note(_Rows(composing), Music(music_id=TRACK, loop=False), 183.0))
    assert "being composed" in ends_composed


def test_a_track_long_enough_or_a_built_in_score_says_nothing():
    assert asyncio.run(music.length_note(_Rows(_track(duration_ms=187_000)), Music(music_id=TRACK), 183.0)) is None
    assert asyncio.run(music.length_note(_Rows(), Music(track="bright-horizon"), 183.0)) is None
    assert asyncio.run(music.length_note(_Rows(), None, 183.0)) is None
    failed = _track(status="failed", duration_ms=None)
    assert asyncio.run(music.length_note(_Rows(failed), Music(music_id=TRACK), 183.0)) is None


def test_an_uploaded_track_is_measured_by_its_own_length(monkeypatch):
    async def thirty(_file_id):
        return 30.0

    monkeypatch.setattr(music, "_file_seconds", thirty)
    note = asyncio.run(music.length_note(_Rows(), Music(file_id=TRACK), 60.0))
    assert note.startswith("Music loops at 30 s of 60 s") and "Upload a longer track" in note


def test_the_render_notes_name_the_tracks_length_against_the_videos():
    looping = music.Bed.of(Music(file_id=TRACK), 120.0)
    assert music.bed_length_note(looping, 90.0) == "Music: the music file lasts 90 s under the 120 s video: it loops at 90 s."
    once = music.Bed.of(Music(music_id=TRACK, loop=False), 120.0)
    assert music.bed_length_note(once, 90.0).endswith(
        "asked to play once, it fades out at 90 s, leaving the video's last 30 s without music."
    )
    long = music.Bed.of(Music(music_id=TRACK, ending="natural"), 120.0)
    assert music.bed_length_note(long, 150.0) == (
        "Music: the AI track lasts 150 s under the 120 s video: its own end lands on the video's end."
    )
    assert music.bed_length_note(music.Bed.of(Music(track="bright-horizon"), 120.0), 0.0) is None


class _Locked:
    async def refresh(self, _row, with_for_update=False):
        return None

    async def flush(self):
        return None


def test_a_ready_track_is_composed_again_in_place_only_to_refit(monkeypatch):
    from app.services.video_studio import jobs

    queued: list = []
    old_file = uuid.uuid4()

    async def no_credits_said(_model):
        return None

    async def resolve(_db, _user_id, *, model=None, provider_id=None, **_kwargs):
        provider = SimpleNamespace(music_credits=no_credits_said)
        return provider, model or "music-model", SimpleNamespace(id=uuid.uuid4(), provider="lyria")

    async def enqueue(_db, kind, row, **options):
        queued.append((kind, options))

    monkeypatch.setattr(music, "resolve", resolve)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    row = SimpleNamespace(
        status=JOB_READY,
        params={"duration_seconds": 120},
        prompt="A calm piano bed",
        model="music-model",
        error=None,
        file_id=old_file,
        duration_ms=120_000,
    )
    user = SimpleNamespace(id=USER_ID)
    with pytest.raises(music.MusicError, match="Only a failed"):
        asyncio.run(music.restart_music(_Locked(), user, row))
    asyncio.run(music.restart_music(_Locked(), user, row, refit=True, duration_seconds=187))
    assert (row.status, row.file_id, row.duration_ms) == (JOB_PENDING, None, None)
    assert row.params["duration_seconds"] == 187 and row.params[music.REPLACED_MEDIA] == [str(old_file)]
    assert queued == [("music", {"generation": 1, "owed_to": None})]
    with pytest.raises(music.MusicError, match="still being composed"):
        asyncio.run(music.restart_music(_Locked(), user, row, refit=True))


# ── 10. A picture is attached once per conversation ───────────────────────


def _file(**fields) -> FileAttachment:
    return FileAttachment(
        **{"id": uuid.uuid4(), "filename": "pic.jpg", "content_type": "image/jpeg", "size": 3, "s3_object_name": "o", **fields}
    )


def test_a_picture_a_tool_shows_is_attached_once_then_named(monkeypatch):
    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as tool

    poster = _file(sha256="poster-bytes", s3_object_name="poster")
    frame = _file(conversation_id=CONVERSATION)
    earlier = _file(conversation_id=CONVERSATION, message_id=uuid.uuid4())
    rows = [poster, frame, earlier]
    copies: dict[str, FileAttachment] = {}
    made: list = []

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

    @asynccontextmanager
    async def session():
        yield _Db()

    async def conversation_copy(_conversation_id, digest):
        return copies.get(digest)

    async def persist(**kwargs):
        made.append(kwargs)
        return _file(conversation_id=CONVERSATION, sha256="poster-bytes")

    async def download(_name):
        return b"bytes"

    from app.services import file_storage as storage

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_conversation_copy", conversation_copy)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    monkeypatch.setattr(storage.file_storage, "download", download)
    shown: set[str] = set()
    first = asyncio.run(
        tool._file_refs([poster.id, frame.id, earlier.id], conversation_id=CONVERSATION, user_id=str(USER_ID), shown=shown)
    )
    copy_id = first.ids[0]
    assert [ref.file_id for ref in first.refs] == [copy_id, str(frame.id)]
    # Shown by a message of an earlier turn: named, never attached again.
    assert first.named == [str(earlier.id)] and len(made) == 1
    copies["poster-bytes"] = SimpleNamespace(id=copy_id)
    again = asyncio.run(
        tool._file_refs([poster.id, frame.id], conversation_id=CONVERSATION, user_id=str(USER_ID), shown=shown)
    )
    assert again.refs == [] and again.named == [copy_id, str(frame.id)] and len(made) == 1
    result = tool._result("Clip ready.", files=again)
    assert result.files is None and f"[[FILE:{copy_id}]]" in result.content and "not attached again" in result.content


# ── 4. A real picture inside a picture ────────────────────────────────────


QUAD = ((100.0, 60.0), (300.0, 80.0), (290.0, 220.0), (110.0, 200.0))


def test_the_perspective_sends_the_quads_corners_onto_the_pictures_corners():
    a, b, c, d, e, f, g, h = pictures.perspective_coefficients(QUAD, 160, 90)
    for (x, y), (u, v) in zip(QUAD, ((0, 0), (160, 0), (160, 90), (0, 90)), strict=True):
        depth = g * x + h * y + 1
        assert (a * x + b * y + c) / depth == pytest.approx(u, abs=1e-6)
        assert (d * x + e * y + f) / depth == pytest.approx(v, abs=1e-6)
    with pytest.raises(pictures.PictureError, match="in a line"):
        pictures.perspective_coefficients(((0, 0), (1, 1), (2, 2), (3, 3)), 10, 10)


def test_a_layer_is_drawn_inside_its_quad_and_nowhere_else():
    base = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    layer = Image.new("RGBA", (160, 90), (255, 0, 0, 255))
    stretched = pictures.compose(base, [(layer, pictures.LayerSpec("f", quad=QUAD, fit="stretch"))])
    assert stretched.size == (400, 300)
    assert stretched.getpixel((200, 140))[:3] == (255, 0, 0) and stretched.getpixel((115, 75))[0] > 200
    assert stretched.getpixel((20, 20))[:3] == (0, 0, 0) and stretched.getpixel((350, 250))[:3] == (0, 0, 0)
    # contain: a 16:9 layer in a squarer quad keeps its shape, the quad's top and bottom left clear.
    contained = pictures.compose(base, [(layer, pictures.LayerSpec("f", quad=QUAD))])
    assert contained.getpixel((200, 140))[:3] == (255, 0, 0) and contained.getpixel((115, 75))[:3] == (0, 0, 0)


def test_a_flat_layer_keeps_its_proportions_its_fit_and_its_opacity():
    base = Image.new("RGBA", (300, 200), (0, 0, 0, 255))
    white = Image.new("RGBA", (100, 50), (255, 255, 255, 255))
    half = pictures.compose(base, [(white, pictures.LayerSpec("f", x=10, y=20, width=200, opacity=0.5))])
    assert half.getpixel((100, 60))[0] in range(120, 136) and half.getpixel((100, 130))[:3] == (0, 0, 0)
    # contain in a square box: the whole layer, centred, the rest transparent.
    boxed = pictures.compose(base, [(white, pictures.LayerSpec("f", x=0, y=0, width=100, height=100, fit="contain"))])
    assert boxed.getpixel((50, 50))[:3] == (255, 255, 255) and boxed.getpixel((50, 10))[:3] == (0, 0, 0)
    covered = pictures.compose(base, [(white, pictures.LayerSpec("f", x=0, y=0, width=100, height=100, fit="cover"))])
    assert covered.getpixel((50, 10))[:3] == (255, 255, 255)


def test_layers_are_checked_field_by_field():
    good = pictures.parse_layers([{"file_id": f"[[FILE:{LOGO}]]", "quad": [list(p) for p in QUAD], "fit": "cover"}])
    assert good[0].file_id == LOGO and good[0].quad == QUAD
    for raw, message in (
        ([], "1 to 4 layers"),
        ([{"file_id": LOGO, "x": 0, "y": 0, "width": 10}] * 5, "at most 4"),
        ([{"x": 0, "y": 0, "width": 10}], "names its picture"),
        ([{"file_id": LOGO}], "placed by x, y and width"),
        ([{"file_id": LOGO, "quad": [[0, 0], [1, 0]]}], "four corners"),
        ([{"file_id": LOGO, "quad": [[0, 0], [1, 0], [2, 0], [3, 0]]}], "encloses no area"),
        ([{"file_id": LOGO, "x": 0, "y": 0, "width": 10, "fit": "fill"}], "fit is one of"),
        ([{"file_id": LOGO, "x": 0, "y": 0, "width": 10, "opacity": 0}], "opacity"),
    ):
        with pytest.raises(pictures.PictureError, match=message):
            pictures.parse_layers(raw)


# ── 8. What a page offers to click ────────────────────────────────────────

ARIA = """- banner:
  - navigation "Main":
    - link "Home"
    - link "Agentic Flow"
    - button "Flow templates"
    - menuitem "Workflows"
- heading "Dashboard" [level=1]
- textbox "Search"
- button "Save \\"draft\\""
- link "Home"
"""


def test_a_pages_interactive_elements_are_read_with_their_accessible_names():
    names = capture.interactive_names(ARIA)
    assert [(e["role"], e["name"]) for e in names] == [
        ("link", "Home"),
        ("link", "Agentic Flow"),
        ("button", "Flow templates"),
        ("menuitem", "Workflows"),
        ("textbox", "Search"),
        ("button", 'Save "draft"'),
    ]
    assert names[1]["selector"] == 'role=link[name="Agentic Flow"]'
    assert names[-1]["selector"] == 'role=button[name="Save \\"draft\\""]'


def test_the_closest_names_to_a_step_rank_every_word_first():
    names = capture.interactive_names(ARIA)
    closest = capture.closest_names("Flows", names)
    # Every word found (flow / flows) first, a name merely alike after.
    assert {e["name"] for e in closest[:2]} == {"Agentic Flow", "Flow templates"} and closest[2]["name"] == "Workflows"
    assert capture.names_line([names[1]]) == '"Agentic Flow" (link)'
    assert capture.closest_names("agentic flow", names)[0]["name"] == "Agentic Flow"
    assert capture.closest_names("Billing", names) == [] and capture.closest_names("", names) == []
