"""A render's chapters: one per scene, where it takes over and its name.

The player marks them on its scrubber and names the one playing, on every
surface a render plays - the theater, a reader's view, the chat card and the
public link. They are planned from the timeline the render drew, never
re-estimated in the browser.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.schemas.video_studio import PublicVideoResponse, VideoChapter
from app.models.video_studio import JOB_READY, VideoProject, VideoPublicLink, VideoRender
from app.services.video_studio import projects, public_links, spec, timeline

pytestmark = pytest.mark.usefixtures("video_studio_enabled")

CLIP = "00000000-0000-0000-0000-00000000000c"
TAKE = "00000000-0000-0000-0000-000000000001"
SHOT = timeline.ClipMedia(duration=6.0, width=1280, height=720, fps=24, frame_count=144)


def _film() -> tuple[spec.VideoSpec, timeline.TimelinePlan]:
    raw = {
        "title": "Normandy",
        "voiceover": {"enabled": False},
        "scenes": [
            {"id": "open", "type": "title", "title": "From Normandy to the World", "duration": 3.0},
            {"id": "dawn", "type": "clip", "clip_id": CLIP, "trim_end": 6.0},
            {"id": "words", "type": "statement", "lines": ["Every *cow* has a  story.", "Ours too."], "duration": 3.0},
            {"id": "demo", "type": "recording", "take_id": TAKE},
            {"id": "close", "type": "outro", "title": "Thanks", "duration": 3.0},
        ],
    }
    parsed = spec.parse_video_spec(spec.validate_video_spec(raw))
    take = timeline.TakeMedia(
        take_id=TAKE,
        duration=8.0,
        css_width=1600,
        css_height=900,
        pixel_ratio=1.0,
        fps=60,
        frame_count=480,
    )
    media = timeline.PlanMedia(clips={"dawn": SHOT}, takes={TAKE: take})
    return parsed, timeline.plan(parsed, media, fps=30, theme={})


def test_every_scene_is_a_chapter_starting_where_it_takes_over():
    parsed, plan = _film()
    found = timeline.chapters(parsed, plan.document, {"dawn": "Dawn over the pastures", "demo": "Sign in"})
    planned = plan.document["scenes"]
    assert found[0]["start"] == 0.0
    for chapter, scene in zip(found[1:], planned[1:], strict=True):
        transition = scene["transition_in"]["duration"]
        assert chapter["start"] == pytest.approx(scene["start"] + transition / 2, abs=1e-3)
    starts = [c["start"] for c in found]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert all(start < plan.duration for start in starts)
    # Named as the editor's timeline names them: the card, the statement's first line, what the scene shows.
    assert [(c["type"], c["title"]) for c in found] == [
        ("title", "From Normandy to the World"),
        ("clip", "Dawn over the pastures"),
        ("statement", "Every cow has a story."),
        ("recording", "Sign in"),
        ("outro", "Thanks"),
    ]
    # Each one is what the schema serves.
    assert [VideoChapter(**c).model_dump() for c in found] == found


def test_a_scene_with_no_name_keeps_its_authored_type_for_the_player_to_word():
    parsed, plan = _film()
    found = timeline.chapters(parsed, plan.document)
    assert (found[1]["type"], found[1]["title"]) == ("clip", None)
    assert (found[3]["type"], found[3]["title"]) == ("recording", None)


def test_a_scene_that_never_takes_over_is_folded_into_the_one_before():
    parsed, plan = _film()
    document = {**plan.document, "scenes": [dict(scene) for scene in plan.document["scenes"]]}
    document["scenes"][2]["start"] = document["scenes"][1]["start"] - 1.0
    document["scenes"][2]["transition_in"] = {"type": "cut", "duration": 0.0}
    found = timeline.chapters(parsed, document)
    assert [c["type"] for c in found] == ["title", "clip", "recording", "outro"]


def test_a_long_name_is_cut_and_its_whitespace_collapsed():
    parsed, plan = _film()
    found = timeline.chapters(parsed, plan.document, {"dawn": "  A  " + "very " * 60})
    assert len(found[1]["title"]) == timeline.CHAPTER_TITLE_MAX
    assert "  " not in found[1]["title"]


def _project() -> VideoProject:
    return VideoProject(id=uuid.uuid4(), user_id=uuid.uuid4(), title="Normandy", spec={}, revision=1, scene_count=5)


def _render(project: VideoProject, **columns) -> VideoRender:
    base = {
        "id": uuid.uuid4(),
        "project_id": project.id,
        "revision": 1,
        "quality": "high",
        "status": JOB_READY,
        "keep": False,
        "file_id": uuid.uuid4(),
        "duration_ms": 30_000,
    }
    base.update(columns)
    return VideoRender(**base)


def test_a_render_and_its_public_link_serve_its_chapters():
    project = _project()
    chapters = [
        {"start": 0.0, "type": "title", "title": "From Normandy"},
        {"start": 3.3, "type": "clip", "title": None},
    ]
    render = _render(project, chapters=chapters)
    assert projects.serialize_render(render)["chapters"] == chapters
    link = VideoPublicLink(id=uuid.uuid4(), project_id=project.id, token_hash="x", token_enc="y", allow_download=False)
    payload = public_links.public_video_payload(project, render, link)
    assert PublicVideoResponse(**payload).model_dump()["chapters"] == chapters
    # A render made before chapters, and a video not rendered yet, have none.
    assert projects.serialize_render(_render(project))["chapters"] == []
    assert public_links.public_video_payload(project, None, link)["chapters"] == []
