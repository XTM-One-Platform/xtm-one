"""Video Studio answers an assignment run can read whole. Synthetic specs, renders and files only.

* every file a render's answer names - its poster, its inserts' crops, its findings' frames, its captions - is in
  the run's file context when the run has no conversation (an assignment run), so ``read_file_content`` opens it in
  that call and every later one; in a conversation nothing changes (they are files of it);
* ``get_video`` pages a long spec by whole scenes: never cut inside one, the scenes left named with the call that
  shows them, ``scenes`` and ``fields`` picking what to show.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services.builtin_tools import video_studio as tool
from app.tools.base import FileContext

OWNER = "00000000-0000-0000-0000-00000000a001"
#: A fixed conversation id: a parameter built at collection time must be the same on every pytest-xdist worker.
CONVERSATION = "00000000-0000-0000-0000-0000000000c1"


def _spec(scenes: int = 24, narration: int = 600) -> dict:
    """A synthetic 24-scene video whose scenes are long enough that the whole spec is over the page."""
    return {
        "title": "Synthetic long video",
        "scenes": [
            {"id": f"s{i + 1}", "type": "statement", "lines": [f"Line {i + 1}"], "narration": "x" * narration}
            for i in range(scenes)
        ],
        "voiceover": {"voice": "v", "pronunciations": [{"term": "SaaS", "say": "sass"}]},
        "music": {"music_id": "00000000-0000-0000-0000-00000000aaaa", "volume": 0.4},
    }


# ── get_video: the spec by whole scenes ───────────────────────────────────


def test_a_spec_that_fits_is_shown_whole_as_before():
    small = _spec(scenes=3, narration=10)
    text = tool.spec_page(small, "v1", selected=[0, 1, 2], fields=set(tool.GET_VIDEO_FIELDS))
    assert text.startswith("Spec as written (edit_video works on it):") and json.dumps(small, indent=1) in text


def test_a_long_spec_is_cut_between_scenes_and_names_the_scenes_left():
    spec = _spec()
    text = tool.spec_page(spec, "v1", selected=list(range(24)), fields=set(tool.GET_VIDEO_FIELDS))
    body = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    shown = [s["id"] for s in body["scenes"]]
    # Whole scenes only, in order, the voiceover and the music always there.
    assert shown == [f"s{i + 1}" for i in range(len(shown))] and 0 < len(shown) < 24
    assert body["scenes"] == spec["scenes"][: len(shown)]
    assert body["voiceover"]["pronunciations"] and body["music"]["volume"] == 0.4
    left = f"{len(shown) + 1}-24"
    assert f"Scenes {left} did not fit this answer" in text and f"get_video(video_id='v1', scenes='{left}')" in text
    assert "edit_video works on the whole spec" in text


def test_scenes_18_to_24_of_a_24_scene_video_come_whole_with_voiceover_and_music():
    spec = _spec()
    selected = tool.scene_selection("18-24", spec["scenes"])
    text = tool.spec_page(spec, "v1", selected=selected, fields=set(tool.GET_VIDEO_FIELDS))
    body = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert [s["id"] for s in body["scenes"]] == [f"s{i}" for i in range(18, 25)]
    assert body["scenes"] == spec["scenes"][17:]
    assert body["voiceover"] == spec["voiceover"] and body["music"] == spec["music"]
    assert "did not fit" not in text and "(scenes 18-24 of 24;" in text


def test_a_scene_larger_than_the_page_is_still_shown_whole():
    spec = _spec(scenes=2, narration=20000)
    text = tool.spec_page(spec, "v1", selected=[0, 1], fields=set(tool.GET_VIDEO_FIELDS))
    body = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert body["scenes"] == spec["scenes"][:1] and "get_video(video_id='v1', scenes='2')" in text


def test_fields_pick_the_keys_of_the_spec_shown():
    spec = _spec()
    text = tool.spec_page(spec, "v1", selected=list(range(24)), fields={"voiceover", "music"})
    body = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert set(body) == {"voiceover", "music"} and "did not fit" not in text
    assert "(no scene (of 24); other keys: voiceover, music)" in text


def test_selectors_take_ids_numbers_and_ranges_and_refuse_what_is_not_there():
    scenes = _spec()["scenes"]
    assert tool.scene_selection("s3, 5,7-8", scenes) == [2, 4, 6, 7]
    assert tool.scene_selection(["22-"], scenes) == [21, 22, 23]
    assert tool.scene_selection(None, scenes) == list(range(24))
    for bad in ("25", "0", "20-30", "nowhere"):
        with pytest.raises(ValueError, match="numbered 1-24"):
            tool.scene_selection(bad, scenes)
    assert tool.scene_ranges([17, 18, 19, 22]) == "18-20,23"
    assert tool.video_fields(None) == set(tool.GET_VIDEO_FIELDS)
    assert tool.video_fields(["Music", "renders"]) == {"music", "renders"}
    assert tool.video_fields(["feedback"]) == {"feedback"}
    with pytest.raises(
        ValueError, match="fields takes spec, scenes, voiceover, music, renders, feedback, revisions; not audio"
    ):
        tool.video_fields("audio")


def test_get_video_says_its_selectors():
    definitions, _executor = tool.build_video_studio_tools(user_id="")
    get_video = next(d for d in definitions if d.name == "get_video")
    assert set(get_video.input_schema["properties"]) == {"video_id", "scenes", "fields", "revision"}
    assert get_video.input_schema["properties"]["fields"]["items"]["enum"] == [
        *tool.GET_VIDEO_FIELDS,
        *tool.GET_VIDEO_ASKED_FIELDS,
    ]
    assert "feedback" not in tool.video_fields(None)
    assert "revisions" not in tool.video_fields(None)
    assert "'18-24'" in get_video.description and "whole scenes" in get_video.description


# ── The render's files, readable by the run that asked ────────────────────


def _ref(name: str) -> FileRef:
    return FileRef(file_id=str(uuid.uuid4()), filename=name, content_type="image/png", size=10, s3_object_name=f"s3/{name}")


def test_in_an_assignment_run_every_file_named_joins_the_runs_context():
    named = tool._Pictures(every=[_ref("poster.jpg"), _ref("crop-0-first.png"), _ref("crop-0-mid.png"), _ref("captions.vtt")])
    context = FileContext()
    tool.register_named_files(context, named)
    ids = [r.file_id for r in named.every]
    # Linked or attached alike, each is a file the run reads: read_file_content resolves it by id.
    assert sorted(r.file_id for r in context.list_files()) == sorted(ids)
    assert all(context.is_fetched(fid) for fid in ids), "read, never handed over as the run's deliverable"
    # Named again by a later get_video_jobs: nothing added twice, nothing replaced.
    tool.register_named_files(context, named)
    assert len(context.list_files()) == len(ids)
    tool.register_named_files(None, named)  # a surface with no file context: nothing to do


def test_pictures_carry_every_file_named_attached_or_linked():
    attached = tool._Pictures(refs=[_ref("a.png")], ids=["a"])
    attached.every = list(attached.refs)
    linked = tool._Pictures(ids=["b"], every=[_ref("b.png")])
    both = attached + linked
    assert [r.filename for r in both.every] == ["a.png", "b.png"] and [r.filename for r in both.refs] == ["a.png"]


@pytest.mark.parametrize("conversation", [None, CONVERSATION], ids=["assignment-run", "conversation"])
def test_the_kept_frames_join_the_runs_context_with_or_without_a_conversation(monkeypatch, conversation):
    """Every kept frame is in the run's context as a file it READ (never a deliverable), in a conversation too:
    sandbox_put_file and code_interpreter's attachments/ find it in the same run (dev:video-frames-sandbox-put)."""
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import _shared
    from app.services.video_studio import clips
    from app.tools.base import ToolCall

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=uuid.UUID(OWNER), role="user")

    async def resolve_frame(_db, _user, **_named):
        return {"kind": "file", "id": "f", "file_id": "f", "name": "launch.mp4", "at_seconds": None}

    async def sweep(_source, _work, **_asked):
        return clips.FrameSweep(frames=[clips.SweepFrame(1.0, b"PNG"), clips.SweepFrame(2.0, b"PNG")], duration=8.0, frame_size=(1920, 1080))

    async def persist(**saved):
        return SimpleNamespace(
            id=uuid.uuid4(), size=3, s3_object_name="s3", **{k: v for k, v in saved.items() if k != "raw_bytes"}
        )

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(clips, "resolve_frame", resolve_frame)
    monkeypatch.setattr(clips, "frame_sweep", sweep)
    monkeypatch.setattr(clips, "frame_grid", lambda tiles: b"SHEET")
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    _definitions, executor = tool.build_video_studio_tools(user_id=OWNER, conversation_id=conversation)
    context = FileContext()
    executor.file_context = context
    result = asyncio.run(
        executor.execute_tool(ToolCall(id="f1", name="save_video_frames", arguments={"file_id": "f", "at_seconds": [1, 2], "keep_frames": True}))
    )
    assert not result.is_error, result.content
    frames = [f["file_id"] for f in json.loads(result.content.split("\n", 1)[0])["frames"]]
    held = {r.file_id for r in context.list_files()}
    assert len(frames) == 2 and set(frames) <= held
    assert all(context.is_fetched(fid) for fid in frames), "read, never handed over as the run's deliverable"
