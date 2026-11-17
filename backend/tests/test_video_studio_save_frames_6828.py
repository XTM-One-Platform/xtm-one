"""Many frames of one video in one call: ``save_video_frames`` and the sweep it is built on.

A QA pass over a render's screen inserts saved one frame per call and was stopped by the loop breaker at the tenth
call on one MP4. ``clips.frame_sweep`` takes every moment from ONE download of the video, ``clips.frame_grid`` lays
the frames out as one labelled contact sheet, and the tool attaches the sheet and names every frame as a file.
Synthetic video only: ffprobe and ffmpeg are faked, each frame painted with the grey of its moment.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services.video_studio import assets, clips, encoder
from app.services.video_studio.render_checks import clock

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a001")
CONVERSATION = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
RENDER_FILE = "00000000-0000-0000-0000-0000000000a2"
SIZE = (1920, 1080)


def _level(at: float | None, duration: float) -> int:
    """The grey a frame of the synthetic video is painted with: its moment, 10 levels a second."""
    return min(250, round((duration if at is None else at) * 10))


@pytest.fixture
def video(monkeypatch):
    """An 8 s 1920x1080 MP4 as ffprobe and ffmpeg see it; the downloads and the seeks counted."""
    world = SimpleNamespace(duration=8.0, downloads=0, seeks=[])

    async def download(file_id, path):
        world.downloads += 1
        Path(path).write_bytes(b"MP4")
        return SimpleNamespace(content_type="video/mp4")

    async def probe(_path):
        return SimpleNamespace(duration=world.duration, width=SIZE[0], height=SIZE[1], fps=30.0, has_audio=False)

    async def extract_still(_path, *, at, out_path, max_side, crop_to=None):
        world.seeks.append(at)
        Image.new("RGB", SIZE, (_level(at, world.duration),) * 3).save(out_path, "PNG")

    monkeypatch.setattr(assets, "download_file", download)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(encoder, "extract_still", extract_still)
    return world


def _source() -> dict:
    return {"kind": "file", "id": RENDER_FILE, "file_id": RENDER_FILE, "name": "launch.mp4", "at_seconds": None}


def _sweep(tmp_path, **asked) -> clips.FrameSweep:
    return asyncio.run(clips.frame_sweep(_source(), str(tmp_path), **asked))


# ── The moments ───────────────────────────────────────────────────────────


def test_a_step_sweep_of_an_eight_second_insert_is_eight_moments():
    assert clips.sheet_moments(8.0, step_seconds=1) == [0, 1, 2, 3, 4, 5, 6, 7]
    assert clips.sheet_moments(20.0, step_seconds=0.5, from_seconds=12, to_seconds=14) == [12.0, 12.5, 13.0, 13.5]
    # The end bounds the range: a to_seconds past it stops at it.
    assert clips.sheet_moments(3.0, step_seconds=1, to_seconds=99) == [0, 1, 2]


def test_listed_moments_are_sorted_and_deduplicated():
    assert clips.sheet_moments(8.0, at_seconds=[4, 1.5, 4, 0]) == [0.0, 1.5, 4.0]


@pytest.mark.parametrize(
    ("asked", "said"),
    [
        ({}, "one of them"),
        ({"at_seconds": [1], "step_seconds": 1}, "one of them"),
        ({"step_seconds": 0}, "above 0"),
        ({"at_seconds": [1], "from_seconds": 2}, "bound a step_seconds sweep"),
        ({"step_seconds": 1, "from_seconds": 9}, "Nothing to sweep"),
        ({"step_seconds": 0.1}, "at most 60 a call"),
        ({"at_seconds": list(range(61))}, "at most 60 a call"),
        ({"at_seconds": [-1]}, "at_seconds must be 0 or more"),
    ],
)
def test_a_sweep_that_cannot_be_taken_says_why(asked, said):
    with pytest.raises(clips.ClipError, match=said):
        clips.sheet_moments(8.0, **asked)


# ── The sweep ─────────────────────────────────────────────────────────────


def test_every_moment_comes_from_one_download_of_the_video(video, tmp_path):
    sweep = _sweep(tmp_path, step_seconds=1)
    assert video.downloads == 1
    assert [f.at_seconds for f in sweep.frames] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert sorted(video.seeks) == [0, 1, 2, 3, 4, 5, 6, 7]
    # Each frame is its own moment's pixels, at the video's own size.
    for frame in sweep.frames:
        with Image.open(io.BytesIO(frame.png)) as image:
            assert image.size == SIZE and image.getpixel((0, 0))[0] == _level(frame.at_seconds, 8.0)
    assert (sweep.duration, sweep.frame_size, sweep.cut) == (8.0, SIZE, None)


def test_a_moment_past_the_end_is_the_last_frame_said_so(video, tmp_path):
    sweep = _sweep(tmp_path, at_seconds=[2, 9.5])
    late = sweep.frames[-1]
    assert (late.at_seconds, late.asked) == (8.0, 9.5) and sweep.frames[0].asked is None
    assert video.seeks.count(None) == 1, "the last frame is read from the end, never seeked past it"


def test_a_region_is_cut_from_every_frame_at_100_percent(video, tmp_path):
    sweep = _sweep(tmp_path, step_seconds=2, box=(0.25, 0.5, 0.75, 1.0))
    assert sweep.cut == (480, 540, 1440, 1080) and sweep.frame_size == SIZE
    for frame in sweep.frames:
        with Image.open(io.BytesIO(frame.png)) as image:
            assert image.size == (960, 540)


def test_a_video_that_is_gone_is_said_so(video, tmp_path, monkeypatch):
    async def gone(file_id, path):
        return None

    monkeypatch.setattr(assets, "download_file", gone)
    with pytest.raises(clips.ClipError, match="is gone"):
        _sweep(tmp_path, step_seconds=1)


# ── The contact sheet ─────────────────────────────────────────────────────


def _png(size: tuple[int, int], level: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (level,) * 3).save(out, "PNG")
    return out.getvalue()


def test_eight_frames_are_one_sheet_of_eight_labelled_tiles():
    tiles = [(clock(t), _png(SIZE, 20 * t)) for t in range(8)]
    with Image.open(io.BytesIO(clips.frame_grid(tiles))) as sheet:
        # 4 columns of 480 px, 2 rows of 270 px + the label strip, 6 px gaps.
        assert sheet.size == (4 * 480 + 5 * 6, 2 * (270 + 26) + 3 * 6)
        # Each tile is its frame, in order: the 6th tile (row 2, column 2) is the frame at 5 s.
        assert sheet.convert("RGB").getpixel((6 + 480 + 6 + 10, 6 + 296 + 6 + 10)) == (100, 100, 100)


def test_a_sheet_of_sixty_tiles_stays_readable_whole():
    tiles = [(clock(t), _png((320, 180), t)) for t in range(60)]
    with Image.open(io.BytesIO(clips.frame_grid(tiles))) as sheet:
        assert sheet.width == 8 * 300 + 9 * 6, "8 columns of 300 px"
    with pytest.raises(clips.ClipError, match="No frame"):
        clips.frame_grid([])


# ── The tool ──────────────────────────────────────────────────────────────


@pytest.fixture
def tool(video, monkeypatch):
    """The Video Studio tools of the owner in a conversation, every file they persist kept in ``stored``."""
    from contextlib import asynccontextmanager

    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as module

    stored: list[SimpleNamespace] = []

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    async def find_user(_db, _user_id):
        return SimpleNamespace(id=OWNER, role="user")

    async def resolve_frame(_db, _user, **named):
        assert named == {"recording_id": None, "clip_id": None, "file_id": RENDER_FILE}
        return _source()

    async def persist(**saved):
        row = SimpleNamespace(
            id=uuid.uuid4(), size=len(saved["raw_bytes"]), s3_object_name="s3", **{k: v for k, v in saved.items() if k != "raw_bytes"}
        )
        stored.append(row)
        return row

    monkeypatch.setattr(module, "async_session", session)
    monkeypatch.setattr(module, "_user", find_user)
    monkeypatch.setattr(clips, "resolve_frame", resolve_frame)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    _definitions, executor = module.build_video_studio_tools(None, str(OWNER), str(CONVERSATION))
    return SimpleNamespace(executor=executor, stored=stored, definitions=_definitions)


def _call(tool, **arguments):
    from app.tools.base import ToolCall

    return asyncio.run(tool.executor.execute_tool(ToolCall(id="f1", name="save_video_frames", arguments=arguments)))


def test_the_tool_writes_one_sheet_and_no_frame_by_default(tool):
    """A sweep keeps ONE file, the sheet; the frames are listed by moment, not stored."""
    result = _call(tool, file_id=RENDER_FILE, step_seconds=1)
    assert not result.is_error, result.content
    head = json.loads(result.content.split("\n", 1)[0])
    assert [f["at_seconds"] for f in head["frames"]] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert all("file_id" not in f for f in head["frames"])
    assert len(tool.stored) == 1 and tool.stored[0].filename.endswith("contact sheet.png")
    assert [f.file_id for f in result.files] == [head["sheet_file_id"]]
    assert "keep_frames true" in result.content


def test_the_tool_attaches_one_sheet_and_names_every_frame(tool):
    result = _call(tool, file_id=RENDER_FILE, step_seconds=1, keep_frames=True)
    assert not result.is_error, result.content
    head = json.loads(result.content.split("\n", 1)[0])
    assert [f["at_seconds"] for f in head["frames"]] == [0, 1, 2, 3, 4, 5, 6, 7]
    # One picture attached, the sheet; every frame a file of the conversation, named in the answer.
    assert [f.file_id for f in result.files] == [head["sheet_file_id"]]
    frames, sheet = tool.stored[:-1], tool.stored[-1]
    assert [str(r.id) for r in frames] == [f["file_id"] for f in head["frames"]]
    assert all(r.conversation_id == str(CONVERSATION) and r.content_type == "image/png" for r in tool.stored)
    assert sheet.filename == "launch - 0.00s-7.00s - contact sheet.png"
    assert "Contact sheet of 8 frames" in sheet.description and "labelled with its moment" in sheet.description
    assert frames[3].description.startswith("The frame at 3.00 s of the video")
    assert f"[[FILE:{head['sheet_file_id']}]]" in result.content


def test_the_tool_cuts_a_region_and_says_its_box(tool):
    result = _call(tool, file_id=RENDER_FILE, at_seconds=[1, 2], region={"box": [0.25, 0.5, 0.75, 1.0]}, keep_frames=True)
    assert not result.is_error, result.content
    assert "cut at 100% (960x540 px of the 1920x1080 frame)" in result.content
    assert tool.stored[0].filename.endswith(" - region.png")


def test_the_tool_refuses_what_cannot_be_swept(tool):
    result = _call(tool, file_id=RENDER_FILE, step_seconds=0.1)
    assert result.is_error and "at most 60 a call" in result.content and tool.stored == []
    bad = _call(tool, file_id=RENDER_FILE, step_seconds=1, region={"box": [0.5, 0.5, 0.2, 0.9]})
    assert bad.is_error and "encloses nothing" in bad.content


def test_the_tool_declares_its_moments_as_positions():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, _executor = build_video_studio_tools(user_id="")
    frames = next(d for d in definitions if d.name == "save_video_frames")
    assert frames.position_budget == clips.SHEET_FRAMES_MOST
    assert {"at_seconds", "step_seconds", "region"} <= set(frames.position_arguments)
    assert frames.input_schema["properties"]["at_seconds"]["maxItems"] == clips.SHEET_FRAMES_MOST
